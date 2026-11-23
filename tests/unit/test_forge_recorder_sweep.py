"""The class sweep of the forge recorder (SP-RECORDER-REDACTS-BY-CLASS 0.20, AC8).

Every judging rule of ``closure.py`` and every reader of ``recording.py`` is
run over the cross-product of its input dimensions, through ``record()`` live
and through the offline transform, and each cell is held to the owner's goal:
a planted private value never survives (replaced or refused), ordinary text is
written byte-identical, a documented residual is written or refused but never
crashes. The expectation of a cell is computed by :func:`expect` from the rule
set stated by construction in D8 and D7, not from the judging code; the sweep
is not independent of the module under test everywhere (0.23): the ``logins``
dimension enumerates the code's own ``LANGUAGE_AT_WORDS``, ``CLASS_WORD_RE``
and ``NEEDLE_MIN_LENGTH`` (D2 and D7 name those tables, so the sweep reads them
rather than copying 425 words), and the string walker decodes JSON stdout with
the code's ``decode_json_values``. The reserved words, route grammars and
registry hosts of ``routes`` are the sweep's own tables.

The whole sweep (``cells()``) is the phase-1 audit of 0.20 and is rerun by
``tmp/fable020/sweep.py``; the unit test runs a deterministic sample of it
(every cell whose id hashes into the sample, at least 500) so the suite stays
short.
"""

from __future__ import annotations

import json
import logging
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from tests.unit import test_forge_recording_acceptance_corpus as corpus

Sample = corpus.Sample

HOST3 = "code.kvncloud.io"
HOST2 = "kvncloud.io"
PUBLIC_HOST = "files.pythonhosted.org"
KNOWN_HOST = "git.zqcorp.io"
KNOWN_REMOTE = f"git@{KNOWN_HOST}:zqgroup/zqmainrepo.git"
NS, REPO = "kvnteam", "kvnbilling"
PUB4 = "104.16.0.1"
PRIV4 = "10.1.2.3"
LOGIN = corpus.FAKE_LOGIN
DENY = corpus.FAKE_DENY

ORDINARY = "ORDINARY"
"""Written, the line byte-identical."""
REWRITE = "REWRITE"
"""Written, not refused; the line may change (a documented placeholder-host rewrite)."""
PLANTED = "PLANTED"
"""Every marker vanishes, or the recording is refused."""
RESIDUAL = "RESIDUAL"
"""Written or refused, never a crash; the markers may survive (a documented NG4 residual)."""
RESIDUAL_UNCHANGED = "RESIDUAL_UNCHANGED"
"""A documented residual that must be written byte-identical (NG4-2, NG4-9, NG4-10)."""
REFUSED = "REFUSED"
"""Refused with the given reason prefix (an owner decision)."""
BOUNDED = "BOUNDED"
"""Written, or refused as ``nesting too deep``, never a crash (0.21, B3: the readers' bound is the interpreter's)."""


@dataclass(frozen=True)
class Cell:
    """One cell of the sweep: a text at a position, what it plants and what the rule set expects."""

    id: str
    rule: str
    text: str
    category: str
    markers: tuple[str, ...] = ()
    note: str = ""
    position: str = "body"
    prefix: str = ""
    remote: str = Sample.remote
    extra: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)


KINDS: dict[str, tuple[str, str]] = {
    "host3": (HOST3, "host3"),
    "host2": (HOST2, "host2"),
    "public_host": (PUBLIC_HOST, "public"),
    "localhost": ("localhost", "placeholder"),
    "example_com": ("www.example.com", "placeholder"),
    "forge_placeholder": (corpus.PLACEHOLDER, "forge_placeholder"),
    "known_host": (KNOWN_HOST, "known"),
    "ipv4_private": (PRIV4, "ipv4_private"),
    "ipv4_public": (PUB4, "ipv4_public"),
    "ipv4_multicast": ("224.0.0.251", "ipv4_private"),
    "ipv4_cgnat": ("100.64.0.1", "ipv4_private"),
    "ipv4_unspecified": ("0.0.0.0", "ipv4_placeholder"),
    "ipv4_broadcast": ("255.255.255.255", "ipv4_placeholder"),
    "ipv4_loopback": ("127.0.0.1", "ipv4_placeholder"),
    "ipv4_rfc5737": ("203.0.113.9", "ipv4_placeholder"),
    "ipv6_global": ("[2a00:1450::1]", "ipv6_global"),
    "ipv6_ula": ("[fd00::1]", "ipv6_private"),
    "ipv6_loopback": ("[::1]", "ipv6_placeholder"),
    "ipv6_doc": ("[2001:db8::1]", "ipv6_placeholder"),
    "ipv6_mapped": (f"[::ffff:{PUB4}]", "ipv6_global"),
    "ipv6_zone": ("[fe80::1%25eth0]", "ipv6_private"),
    "version": ("1.8.0.252", "version"),
    "cidr_private": ("10.0.0.0/8", "cidr_private"),
    "cidr_public": ("1.2.3.4/24", "cidr_public"),
    "dotted_code": ("response.data.id", "code"),
    "dotted_call": ("self.logger.info", "code"),
    "reverse_dns": ("com.example.app", "reverse_dns"),
}
"""Token kind -> (the token, its class for :func:`expect`)."""

LEADS: dict[str, tuple[str, str]] = {
    "none": ("see ", "none"),
    "http": ("see http://", "scheme"),
    "https": ("see https://", "scheme"),
    "ssh_git": ("ssh://git@", "scheme_at"),
    "slashes": ("src=//", "slashes"),
    "at": ("ssh deploy@", "at"),
    "scp": ("git clone git@", "scp"),
    "slash": ("x/", "slash"),
    "eq": ("HOST=", "none"),
    "colon": ("host:", "none"),
    "quote": ('"', "none"),
    "paren": ("(", "none"),
    "angle": ("<", "none"),
    "mdlink": ("[x](", "none"),
}
"""Lead name -> (the prefix written before the token, its lead class)."""

FOLLOWS: dict[str, tuple[str, str]] = {
    "none": (" now", "none"),
    "seg": (f"/{NS} now", "seg"),
    "segs": (f"/{NS}/{REPO}/x now", "segs"),
    "port": (":8443 now", "port"),
    "portpath": (f":8443/{NS}/{REPO} now", "portpath"),
    "dcolon": (f"::{NS}/{REPO} now", "none"),
    "slash_end": ("/ now", "none"),
    "dot_end": (". Done", "none"),
}
"""Follow name -> (the suffix written after the token, its follow class)."""

CLOSERS = {"quote": '"', "paren": ")", "angle": ">", "mdlink": ")"}

NG4_2 = "NG4-2 a bare or followed private address is written"
NG4_3 = "NG4-3 a two-label host un-led"
NG4_8 = "NG4-8 a three-label host bare in prose"
NG4_9 = "NG4-9 a bare public IPv4, or one that is a path segment"
NG4_10 = "NG4-10 an un-led IPv6 literal"
NG4_11 = "NG4-11 a lone path segment after a host is a page, not an identity"
NG4_12 = "NG4-12 a path after a placeholder-class host is not read"
NG4_13 = "NG4-13 a mention-only handle equal to a language word is written in its @ form"


def _led(lead: str) -> bool:
    return lead in ("scheme", "scheme_at", "slashes", "at", "scp")


def _followed(follow: str) -> bool:
    return follow in ("seg", "segs", "port", "portpath")


def _path_markers(follow: str) -> tuple[str, ...]:
    """The namespace and repository the follow writes, when it writes two segments."""
    return (NS, REPO) if follow in ("segs", "portpath") else ()


def expect(kind: str, lead: str, follow: str) -> tuple[str, tuple[str, ...], str, str]:
    """``(category, markers, note, refusal prefix)`` the rule set gives a token kind, led and followed so.

    The rules, by construction (D8, D7 source 3, D1b, 0.20):

    * a host-shaped name is a host only when led (a scheme, ``//``, ``<local>@``) or followed
      (``/<segment>``, ``:<port>``); two labels need a scheme or ``@``; bare it is NG4-8 / NG4-3;
    * an IPv4 quad follows the host rule, outside a version context (a single ``/`` before it,
      a comparator, ``version``, a ``-``/``+`` after it); led and public or private it is a host
      and is replaced; followed and public it is refused; followed or bare and private it is
      written as it stands (NG4-2); bare and public it is written (NG4-9);
    * a bracketed IPv6 literal led is a host; un-led and global it is refused; un-led and private
      it is written (NG4-10); the loopback, unspecified and documentation literals name no one;
    * a placeholder-class or public host is never a value and nothing is read after it;
    * the path after a led host, or after a followed label host, yields a namespace and a
      repository only when it has both (two segments) or is an API route; a lone segment is a
      page (NG4-11); a followed address has no path read (NG4-2, the orchestrator's 0.19 rule);
    * a mailbox ``<local>@<host>`` is a literal on a documentation domain, on a placeholder
      address, and as the four noreply placeholders; ``<local>@forge.example.com`` and
      ``<local>@<subdomain of example.com>`` are refused (NG4-5, D8); a known or unknown private
      host after ``@`` is replaced, then the mailbox refused; ``<local>@localhost`` is one label
      and never a mailbox;
    * the convenience layer (D1b) rewrites every URL that carries a userinfo, every scp remote
      (``<user>@<host>:<path>``, a port and a path included) not on an example host, and every
      noreply mailbox; those are documented rewrites, never refusals.
    """
    token, cls = KINDS[kind]
    led, followed = _led(lead), _followed(follow)
    path = _path_markers(follow)
    scp_shape = lead == "at" and follow == "portpath"
    kept = cls in ("public", "placeholder", "forge_placeholder", "ipv4_placeholder")
    if kept and (lead == "scheme_at" or scp_shape):
        return REWRITE, (), "a URL with a userinfo, or an scp remote, is always rewritten", ""
    if cls == "public":
        if lead == "at":
            return REFUSED, (), "a mailbox on a public host is personal", "unknown mailbox"
        if lead == "scp":
            return REWRITE, (), "an scp remote is always written as git@forge.example.com", ""
        return ORDINARY, (), "", ""
    if cls == "placeholder":
        if lead == "at" and "." in token:
            return REFUSED, (), "D8: a mailbox on a subdomain of example.com", "unknown mailbox"
        return ORDINARY, (), "git@<example host>:<path> is kept" if lead == "scp" else "", ""
    if cls == "forge_placeholder":
        if lead == "at":
            return REFUSED, (), "NG4-5 <local>@forge.example.com is refused", "unknown mailbox"
        if lead in ("scheme", "scp"):
            return REWRITE, (), "a URL on forge.example.com gets the placeholder path", ""
        return ORDINARY, (), "", ""
    if cls == "ipv4_placeholder":
        return (
            ORDINARY,
            (),
            "a placeholder address names no one, after @ as well" if lead == "at" else "",
            "",
        )
    if cls == "ipv6_placeholder":
        if lead == "scheme_at":
            return REWRITE, (), "a URL with a userinfo is always rewritten", ""
        return ORDINARY, (), NG4_12 if follow in ("seg", "segs", "portpath") else "", ""
    if cls == "known":
        markers = (token, *path) if (led or followed) else (token,)
        return PLANTED, markers, "a known value is replaced everywhere", ""
    if cls == "host3":
        if led or followed:
            return PLANTED, (token, *path), NG4_11 if follow == "seg" else "", ""
        return RESIDUAL_UNCHANGED, (token,), NG4_8, ""
    if cls == "host2":
        if lead in ("scheme", "scheme_at", "at", "scp"):
            return PLANTED, (token, *path), "", ""
        return RESIDUAL_UNCHANGED, (token,), NG4_3, ""
    if cls == "ipv4_private":
        if lead == "slash":
            return ORDINARY, (), "a quad after a single / is a path segment", ""
        if led:
            return PLANTED, (token, *path), "", ""
        return RESIDUAL_UNCHANGED, (token,), NG4_2, ""
    if cls == "ipv4_public":
        if lead == "slash":
            return RESIDUAL_UNCHANGED, (token,), NG4_9, ""
        if led:
            return PLANTED, (token, *path), "", ""
        if followed:
            return REFUSED, (), "a followed public address", "public address"
        return RESIDUAL_UNCHANGED, (token,), NG4_9, ""
    if cls == "ipv6_global":
        if led:
            return PLANTED, (token, *path), "", ""
        return REFUSED, (), "an un-led global literal", "public address"
    if cls == "ipv6_private":
        if led:
            return PLANTED, (token, *path), "", ""
        return RESIDUAL_UNCHANGED, (token,), NG4_10, ""
    if cls == "version":
        if led:
            return PLANTED, (token,), "a led quad is an address, whatever its digits", ""
        if lead == "slash":
            return ORDINARY, (), "a quad after a single / is a version", ""
        if followed:
            return (
                REFUSED,
                (),
                "a followed quad is an address (owner decision 0.19)",
                "public address",
            )
        return ORDINARY, (), "", ""
    if cls == "cidr_private":
        if led:
            return PLANTED, ("10.0.0.0",), "", ""
        return ORDINARY, (), "a private network is written", ""
    if cls == "cidr_public":
        if led:
            return PLANTED, ("1.2.3.4",), "", ""
        if lead == "slash":
            return ORDINARY, (), "a quad after a single / is a version", ""
        return (
            REFUSED,
            (),
            "a public CIDR is a followed address (owner decision 0.19)",
            "public address",
        )
    if cls == "code":
        if led or followed:
            return PLANTED, (token,), "a dotted name led or followed is a host (owner decision)", ""
        return ORDINARY, (), "", ""
    if cls == "reverse_dns":
        if lead in ("scheme", "scheme_at", "at", "scp"):
            return PLANTED, (token,), "H1: any run after a scheme or @ is a host", ""
        return ORDINARY, (), "a reverse-DNS head is never a host un-led", ""
    raise AssertionError(kind)


def _line(lead: str, kind: str, follow: str) -> str:
    """The text of a grid cell."""
    prefix = LEADS[lead][0]
    token = KINDS[kind][0]
    suffix = FOLLOWS[follow][0]
    if lead == "scp":
        suffix = f":{NS}/{REPO}.git" + suffix
    if lead in CLOSERS:
        body, _, tail = suffix.partition(" ")
        suffix = f"{body}{CLOSERS[lead]} {tail}"
    return f"{prefix}{token}{suffix}"


def _versioned_leads() -> set[str]:
    return {"none", "slash", "eq", "colon", "quote", "paren", "angle", "mdlink"}


def grid_cells() -> list[Cell]:
    """Every lead x token kind x follow of the host, address and version rules, in the body."""
    out: list[Cell] = []
    for kind, (_token, cls) in KINDS.items():
        leads = list(LEADS)
        if cls in ("version", "cidr_private", "cidr_public", "code", "reverse_dns"):
            leads = [lead for lead in LEADS if lead in _versioned_leads() or lead == "http"]
        if cls == "reverse_dns":
            leads.append("slashes")
        for lead in leads:
            for follow in FOLLOWS:
                if (cls.startswith("cidr") or lead == "scp") and follow != "none":
                    continue
                lead_cls, follow_cls = LEADS[lead][1], FOLLOWS[follow][1]
                category, markers, note, prefix = expect(kind, lead_cls, follow_cls)
                if lead == "scp" and category == PLANTED:
                    markers = tuple(dict.fromkeys((*markers, NS, REPO)))
                rule = (
                    "ipv6"
                    if cls.startswith("ipv6")
                    else "ipv4"
                    if "4" in cls or "cidr" in cls or cls == "version"
                    else "hosts"
                )
                out.append(
                    Cell(
                        id=f"grid:{rule}:{kind}:{lead}:{follow}",
                        rule=rule,
                        text=_line(lead, kind, follow),
                        category=category,
                        markers=markers,
                        note=note,
                        prefix=prefix,
                        remote=KNOWN_REMOTE if cls == "known" else Sample.remote,
                    )
                )
    return out


def version_context_cells() -> list[Cell]:
    """The version contexts of a quad: a v prefix, a comparator, the word version, a tag; never an address."""
    out: list[Cell] = []
    shapes = {
        "v_prefix": "v1.8.0.252",
        "comparator": "pkg == 1.8.0.252",
        "comparator_spaces": "pkg  >=   1.8.0.252",
        "word": "version 1.8.0.252",
        "word_colon": "Version: 1.8.0.252",
        "tag_rc": "1.8.0.252-rc1",
        "tag_build": "1.8.0.252+build.7",
        "five_parts": "10.0.19045.4529",
        "maven_segment": "https://repo.maven.apache.org/maven2/x/y/19.3.0.0/y-19.3.0.0.pom",
        "pypi_segment": "https://pypi.org/project/x/1.2.3.4/files",
        "nuget_segment": "https://api.nuget.org/v3-flatcontainer/x/1.2.3.4/x.1.2.3.4.nupkg",
    }
    for name, text in shapes.items():
        for follow in ("none", "seg", "port", "dot_end"):
            suffix = FOLLOWS[follow][0]
            if name.endswith("_segment") and follow != "none":
                continue
            out.append(
                Cell(
                    id=f"version:{name}:{follow}",
                    rule="ipv4",
                    text=f"Deployed {text}{suffix}",
                    category=ORDINARY,
                    note="a version context is never an address",
                )
            )
    return out


ENCODERS: dict[str, dict[str, str]] = {
    "pct": {":": "%3A", "/": "%2F", "@": "%40"},
    "pct_dots": {":": "%3A", "/": "%2F", "@": "%40", ".": "%2E"},
    "pct2": {":": "%253A", "/": "%252F", "@": "%2540"},
    "entity": {":": "&#58;", "/": "&#47;", "@": "&#64;", ".": "&#46;"},
    "uescape": {":": "\\u003a", "/": "\\u002f", "@": "\\u0040", ".": "\\u002e"},
    "bslash": {"/": "\\/"},
    "xescape": {"/": "\\x2f", ":": "\\x3a"},
}
"""Encoding name -> the characters it encodes and how."""


def _encode(text: str, encoder: str) -> str:
    table = ENCODERS[encoder]
    return "".join(table.get(c, c) for c in text)


def encoded_cells() -> list[Cell]:
    """A subset of the grid in every decoded form: the outcome is the raw one's, a refusal allowed."""
    out: list[Cell] = []
    kinds = (
        "host3",
        "host2",
        "known_host",
        "ipv4_public",
        "ipv4_private",
        "public_host",
        "localhost",
        "version",
    )
    for kind in kinds:
        for lead in ("none", "https", "slashes", "at"):
            if kind == "version" and lead != "none":
                continue
            for follow in ("none", "segs", "port"):
                category, markers, note, prefix = expect(kind, LEADS[lead][1], FOLLOWS[follow][1])
                core = _line(lead, kind, follow).removeprefix(LEADS[lead][0]).removesuffix(" now")
                core = (
                    LEADS[lead][0].removeprefix("see ").removeprefix("src=").removeprefix("ssh ")
                    + core
                )
                for encoder in ENCODERS:
                    encoded = _encode(core, encoder)
                    if encoded == core:
                        continue
                    text = f"see {encoded} now"
                    cat = category
                    if category in (RESIDUAL_UNCHANGED, ORDINARY):
                        cat = category
                    if category == REFUSED:
                        cat = REFUSED
                    out.append(
                        Cell(
                            id=f"decoded:{kind}:{lead}:{follow}:{encoder}",
                            rule="decoded",
                            text=text,
                            category=cat,
                            markers=tuple(m for m in markers if m in text) or markers,
                            note=note,
                            prefix=prefix,
                            remote=KNOWN_REMOTE if kind == "known_host" else Sample.remote,
                        )
                    )
    return out


POSITIONS: tuple[str, ...] = (
    "body",
    "title",
    "stderr",
    "stdin",
    "stdout_text",
    "diff_plus",
    "diff_minus",
    "diff_context",
    "args",
    "kwargs",
    "json_value",
    "json_list",
    "json_key",
    "ci_log",
)


def position_cells() -> list[Cell]:
    """A small set of shapes in every position of a fixture."""
    shapes: list[tuple[str, str, str, str]] = [
        ("host3_url", "host3", "https", "segs"),
        ("host3_bare", "host3", "none", "none"),
        ("host3_port", "host3", "none", "port"),
        ("known_bare", "known_host", "none", "none"),
        ("public4_port", "ipv4_public", "none", "port"),
        ("public4_url", "ipv4_public", "http", "segs"),
        ("private4_port", "ipv4_private", "none", "portpath"),
        ("version_bare", "version", "none", "none"),
        ("public_url", "public_host", "https", "segs"),
        ("localhost_url", "localhost", "http", "portpath"),
        ("ipv6_doc_url", "ipv6_doc", "http", "segs"),
        ("ipv6_global_bare", "ipv6_global", "none", "port"),
    ]
    out: list[Cell] = []
    for name, kind, lead, follow in shapes:
        category, markers, note, prefix = expect(kind, LEADS[lead][1], FOLLOWS[follow][1])
        text = _line(lead, kind, follow)
        for position in POSITIONS:
            if position == "json_key" and category == REFUSED:
                continue
            cat, mk, why = category, markers, prefix
            if position == "json_key" and category == PLANTED:
                cat, mk, why = REFUSED, (), "residual"
            out.append(
                Cell(
                    id=f"position:{name}:{position}",
                    rule="positions",
                    text=text,
                    category=cat,
                    markers=mk,
                    note=note if position != "json_key" else "a key is judged, never rewritten",
                    position=position,
                    prefix=why,
                    remote=KNOWN_REMOTE if kind == "known_host" else Sample.remote,
                )
            )
    return out


def known_value_cells() -> list[Cell]:
    """Every known value of D7 sources 1 and 2, in every position and decoded form."""
    values: list[tuple[str, str, str, str]] = [
        ("login", LOGIN, PLANTED, ""),
        ("host", KNOWN_HOST, PLANTED, ""),
        ("namespace_repo", "zqgroup/zqmainrepo", PLANTED, ""),
        ("uid", str(corpus.FAKE_UID), PLANTED, ""),
        ("node_legacy", corpus._node_legacy(corpus.FAKE_UID), PLANTED, ""),
        ("node_new", corpus._node_new(corpus.FAKE_UID), PLANTED, ""),
        ("deny", DENY, REFUSED, "deny-list entry"),
    ]
    out: list[Cell] = []
    unlearnt = ("stdout_text", "diff_plus", "diff_minus", "diff_context", "ci_log")
    for name, value, category, prefix in values:
        for position in POSITIONS:
            if position == "json_key":
                continue
            if position in unlearnt and name in ("login", "uid", "node_legacy", "node_new"):
                continue
            for encoder in ("raw", "pct_dots", "entity", "uescape"):
                encoded = value if encoder == "raw" else _encode(value, encoder)
                if encoder != "raw" and encoded == value:
                    continue
                markers = tuple(value.split("/")) if category == PLANTED else ()
                out.append(
                    Cell(
                        id=f"known:{name}:{position}:{encoder}",
                        rule="known",
                        text=f"see {encoded} here",
                        category=category,
                        markers=markers,
                        note="D7: a known value is replaced everywhere, an encoded form refused",
                        position=position,
                        prefix=prefix,
                        remote=KNOWN_REMOTE,
                    )
                )
    return out


def mailbox_cells() -> list[Cell]:
    """Every local x host class of the mailbox rule."""
    locals_: dict[str, str] = {
        "person": "deploy",
        "git": "git",
        "noreply": "noreply",
        "no_reply": "no-reply",
        "class_word": "author",
        "credential": "x-access-token:${GITHUB_TOKEN}",
        "mailto": "mailto:deploy",
    }
    hosts: dict[str, tuple[str, str]] = {
        "host3": (HOST3, "private"),
        "host2": (HOST2, "private"),
        "known": (KNOWN_HOST, "private"),
        "ipv4_private": (PRIV4, "private"),
        "ipv4_public": (PUB4, "private"),
        "localhost": ("localhost", "doc"),
        "example_com": ("example.com", "doc"),
        "sub_example": ("mail.example.com", "sub_example"),
        "forge": (corpus.PLACEHOLDER, "forge"),
        "noreply_forge": (f"users.noreply.{corpus.PLACEHOLDER}", "noreply_forge"),
        "loopback": ("127.0.0.1", "placeholder_address"),
        "unspecified": ("0.0.0.0", "placeholder_address"),
        "rfc5737": ("203.0.113.9", "placeholder_address"),
        "public": (PUBLIC_HOST, "public"),
        "dot_test": ("mail.test", "doc"),
    }
    out: list[Cell] = []
    for lname, local in locals_.items():
        for hname, (host, hcls) in hosts.items():
            box = f"{local}@{host}"
            bare_local = local.removeprefix("mailto:")
            credential = ":" in bare_local
            if hcls == "private":
                category, markers, note, prefix = (
                    PLANTED,
                    (host,),
                    "the host is replaced, then the mailbox refused",
                    "",
                )
            elif credential and hname != "localhost":
                category, markers, note, prefix = (
                    REFUSED,
                    (),
                    "a credential is refused everywhere",
                    "unknown mailbox",
                )
            elif hcls == "noreply_forge" or (
                hcls == "public" and bare_local in ("noreply", "no-reply")
            ):
                category, markers, note, prefix = (
                    REWRITE,
                    (),
                    "D1b: a noreply mailbox is normalised",
                    "",
                )
            elif hname == "localhost":
                category, markers, note, prefix = ORDINARY, (), "one label is never a mailbox", ""
            elif hcls == "public":
                category, markers, note, prefix = (
                    REFUSED,
                    (),
                    "a mailbox on a public host",
                    "unknown mailbox",
                )
            elif credential:
                category, markers, note, prefix = (
                    REFUSED,
                    (),
                    "a credential is refused everywhere",
                    "unknown mailbox",
                )
            elif hcls == "doc":
                category, markers, note, prefix = ORDINARY, (), "", ""
            elif hcls == "sub_example":
                category, markers, note, prefix = (
                    (ORDINARY, (), "git@<placeholder-class host>", "")
                    if bare_local == "git"
                    else (REFUSED, (), "a mailbox on a subdomain of example.com", "unknown mailbox")
                )
            elif hcls == "placeholder_address":
                category, markers, note, prefix = (
                    ORDINARY,
                    (),
                    "a mailbox on a placeholder address names no one",
                    "",
                )
            elif hcls == "forge":
                category, markers, note, prefix = (
                    (ORDINARY, (), "a placeholder mailbox", "")
                    if bare_local in ("git", "noreply", "no-reply")
                    else (REFUSED, (), "NG4-5", "unknown mailbox")
                )
            else:
                category, markers, note, prefix = (
                    (ORDINARY, (), "a noreply placeholder", "")
                    if bare_local == "author"
                    else (REFUSED, (), "NG4-5", "unknown mailbox")
                )
            out.append(
                Cell(
                    id=f"mailbox:{lname}:{hname}",
                    rule="mailbox",
                    text=f"contact {box} today",
                    category=category,
                    markers=markers,
                    note=note,
                    prefix=prefix,
                    remote=KNOWN_REMOTE if hname == "known" else Sample.remote,
                )
            )
    return out


def mention_cells() -> list[Cell]:
    """Every ``@<word>`` class of the mention finder: handles, decorators, at-rules, tags, packages."""
    handles: list[tuple[str, str, str, str]] = [
        ("known_login", LOGIN, PLANTED, "a known login"),
        ("unknown_handle", "zqsomeone", PLANTED, "a mention-only login is replaced in its @ form"),
        (
            "short_handle",
            "bob",
            PLANTED,
            "a short handle is replaced in its @ form, never a refusal",
        ),
        ("decorator", "dataclass", ORDINARY, "a standard-library decorator"),
        ("decorator_case", "DataClass", ORDINARY, "case-insensitive"),
        ("java_override", "Override", ORDINARY, "a java.lang annotation"),
        ("java_suppress", "SuppressWarnings", ORDINARY, "a java.lang annotation"),
        ("kotlin_jvmstatic", "JvmStatic", ORDINARY, "a Kotlin standard-library annotation"),
        ("css_font_face", "font-face", ORDINARY, "a CSS at-rule"),
        ("css_media", "media", ORDINARY, "a CSS at-rule"),
        ("css_color_profile", "color-profile", ORDINARY, "a CSS at-rule"),
        ("css_custom_media", "custom-media", ORDINARY, "a PostCSS at-rule"),
        ("tailwind_apply", "apply", ORDINARY, "a Tailwind at-rule"),
        ("tailwind_config", "config", ORDINARY, "a Tailwind at-rule"),
        ("sass_use", "use", ORDINARY, "a Sass at-rule"),
        ("jsdoc_param", "param", ORDINARY, "a JSDoc tag"),
        ("jsdoc_returns", "returns", ORDINARY, "a JSDoc tag"),
        ("javadoc_throws", "throws", ORDINARY, "a Javadoc tag"),
        ("javadoc_inheritdoc", "inheritDoc", ORDINARY, "a Javadoc tag"),
        ("phpdoc_var", "var", ORDINARY, "a PHPDoc tag"),
        ("swift_objc", "objc", ORDINARY, "a Swift attribute"),
        ("swift_escaping", "escaping", ORDINARY, "a Swift attribute"),
        ("dart_required", "required", ORDINARY, "a Dart annotation"),
        ("scoped_package", "types/node", ORDINARY, "a scoped package"),
        ("dotted", "pytest.fixture", ORDINARY, "a dotted name"),
        ("call", "lru_cache(maxsize=1)", ORDINARY, "a decorator call"),
        ("non_person", "github-actions", ORDINARY, "a non-person login"),
        ("class_word", "author", ORDINARY, "a class word"),
    ]
    out: list[Cell] = []
    for name, handle, category, note in handles:
        for carrier in ("cc @{} please", "@{} wrote:", "ping @{}.", "(@{})"):
            text = carrier.format(handle)
            out.append(
                Cell(
                    id=f"mention:{name}:{carrier.split('@')[0].strip() or 'start'}:{carrier.count('.')}{carrier.count('(')}",
                    rule="mentions",
                    text=text,
                    category=category,
                    markers=(handle,) if category == PLANTED else (),
                    note=note,
                )
            )
    return out


CARRIERS: list[tuple[str, str, str, str]] = [
    ("curl_install", "curl -fsSL https://{host}/{ns}/{repo}/install.sh | sh", "scheme", "segs"),
    (
        "curl_connect_fail",
        "curl: (7) Failed to connect to {host} port 443 after 2 ms",
        "none",
        "none",
    ),
    ("curl_port_path", "curl {host}:8080/metrics", "none", "port"),
    ("docker_pull", "docker pull {host}:5000/{ns}/{repo}:1.2.3", "none", "segs"),
    ("dockerfile_from", "FROM {host}/{ns}/{repo}:3.12-slim", "none", "segs"),
    ("pip_index", "Looking in indexes: https://{host}/simple", "scheme", "seg"),
    (
        "pip_download",
        "Downloading https://{host}/packages/ab/cd/12.6.1.1/x-1.2.3.4-py3-none-any.whl",
        "scheme",
        "segs",
    ),
    (
        "maven_download",
        "[INFO] Downloading from nexus: https://{host}/repository/maven-public/com/kvn/{repo}/19.3.0.0/{repo}-19.3.0.0.pom",
        "scheme",
        "segs",
    ),
    ("npm_registry", "npm config set registry https://{host}/api/npm/", "scheme", "segs"),
    ("npm_404", "npm ERR! 404 Not Found - GET https://{host}/@{ns}%2f{repo}", "scheme", "seg"),
    (
        "nuget_source",
        "dotnet nuget add source https://{host}/v3/index.json -n kvn",
        "scheme",
        "segs",
    ),
    (
        "go_get",
        "go: {host}/{ns}/{repo}@v1.2.3: reading https://{host}/{ns}/{repo}?go-get=1: 403",
        "scheme",
        "segs",
    ),
    ("go_require", "require {host}/{ns}/{repo} v1.9.0", "none", "segs"),
    (
        "cargo_registry",
        '[registries.kvn]\nindex = "https://{host}/{ns}/crates-index"',
        "scheme",
        "segs",
    ),
    (
        "gradle_get",
        "> Could not GET 'https://{host}/artifactory/libs-release/com/kvn/{repo}/1.0.0/{repo}-1.0.0.pom'.",
        "scheme",
        "segs",
    ),
    (
        "uvicorn_running",
        "INFO:     Uvicorn running on http://{host}:8000 (Press CTRL+C to quit)",
        "scheme",
        "port",
    ),
    ("uvicorn_access", 'INFO:     {host}:51234 - "GET /health HTTP/1.1" 200 OK', "none", "port"),
    ("nginx_upstream", 'upstream: "http://{host}:8080/api/v1/health"', "scheme", "segs"),
    ("nginx_server_name", "server_name {host};", "none", "none"),
    ("k8s_image", "image: {host}:5000/{ns}/{repo}:1.2.3", "none", "segs"),
    (
        "k8s_dial",
        'Get "https://{host}:443/api": dial tcp {host}:443: i/o timeout',
        "scheme",
        "port",
    ),
    (
        "k8s_pull_fail",
        'Failed to pull image "{host}/{ns}/{repo}:latest": rpc error',
        "none",
        "segs",
    ),
    (
        "terraform_source",
        'source = "git::https://{host}/{ns}/{repo}.git?ref=v1.0.0"',
        "scheme",
        "segs",
    ),
    ("systemd_env", "Environment=API_URL=https://{host}:8443/{ns}/{repo}", "scheme", "segs"),
    (
        "journald_sshd",
        "Nov 22 10:00:01 build-01 sshd[1234]: Accepted publickey for deploy from {host} port 51234 ssh2",
        "none",
        "none",
    ),
    (
        "journald_unit",
        "Nov 22 10:00:02 build-01 systemd[1]: Started {repo}.service - {host}.",
        "none",
        "none",
    ),
    ("py_url", 'url = f"https://{host}/api/v1/{ns}/{repo}"', "scheme", "segs"),
    ("js_cdn", "const CDN = '//{host}/{ns}/{repo}/app.js';", "slashes", "segs"),
    ("ts_fetch", "await fetch(`https://{host}/graphql`, {{ method: 'POST' }});", "scheme", "seg"),
    ("go_comment", "x := 1 //{host}/env/prod", "slashes", "segs"),
    ("java_url", 'String url = "https://{host}/{ns}/{repo}";', "scheme", "segs"),
    ("rust_url", 'let url = "https://{host}/{ns}";', "scheme", "seg"),
    ("c_define", '#define API_HOST "{host}"', "none", "none"),
    ("ruby_uri", 'uri = URI("https://{host}/{ns}/{repo}")', "scheme", "segs"),
    ("yaml_url", "server:\n  url: https://{host}/{ns}/{repo}\n  timeout: 30s", "scheme", "segs"),
    ("toml_url", '[server]\nurl = "https://{host}/{ns}/{repo}"', "scheme", "segs"),
    ("json_url", '{{"url": "https://{host}/{ns}/{repo}", "retries": 3}}', "scheme", "segs"),
    ("ssh_config", "Host build\n  HostName {host}\n  User deploy", "none", "none"),
    ("hosts_file", "10.0.0.5 {host}", "none", "none"),
    (
        "prose_fr",
        "La version 1.2.3.4 est déployée sur https://{host}/{ns}/{repo}.",
        "scheme",
        "segs",
    ),
    ("prose_de", "Bitte {host} prüfen: Version 1.2.3.4 ist draußen.", "none", "none"),
    ("prose_es", "El servidor {host}:8443 no responde desde ayer.", "none", "port"),
    ("prose_ja", "サーバー {host} に接続できません。", "none", "none"),
    ("prose_zh", "请检查 https://{host}/{ns}/{repo} 的日志。", "scheme", "segs"),
    ("prose_ru", "Сервер {host}:8443/{ns} недоступен.", "none", "seg"),
]
"""Realistic carriers: (name, template, the lead class of ``{host}``, the follow class of ``{host}``)."""

CARRIER_HOSTS: dict[str, str] = {
    "host3": "host3",
    "public_host": "public_host",
    "localhost": "localhost",
    "ipv4_private": "ipv4_private",
    "ipv4_public": "ipv4_public",
    "known_host": "known_host",
    "ipv6_doc": "ipv6_doc",
}

ORDINARY_CODE: tuple[str, ...] = (
    "const userId = response.data.id;",
    'self.logger.info("started")',
    'cfg.Server.Host = os.Getenv("HOST")',
    "window.app.config.dev = true;",
    "arr.items.at(-1)",
    "import os.path",
    "open Xcode.app and config.dev",
    "ssl.ca, stream.io and torch.net are modules",
    "We run JDK 1.8.0.252 and dotnet 4.8.1.0 in prod",
    "subnet 192.168.1.0/24 and 172.16.0.0/12",
    "Listening on 0.0.0.0:8000 (press CTRL+C to quit)",
    "resolver 1.1.1.1 8.8.8.8 valid=300s;",
    "image: registry.k8s.io/pause:3.9",
    "Step 1/7 : FROM python:3.12",
    "Receiving objects: 100% (1234/1234), 1.2 MiB | 4.5 MiB/s",
    'implementation("com.squareup.okhttp3:okhttp:4.12.0")',
    "org.apache.kafka:kafka-clients:3.7.0 and io.netty:netty-all:4.1",
    "Running with gitlab-runner 17.0.0 (abc1234)",
    "Successfully installed pip-24.0 setuptools-69.5.1",
    "Terraform v1.9.5 on linux_amd64",
    "rsync -av src/ build-01::module/ and see man.7.org",
    "src/settings.local.py:10:5: E501 line too long",
    "config.h.in:5: error: expected ';'",
    "kernel/foo.bar.cu:12:3: error: undefined",
    "top/core.alu.sv:33: syntax error",
    "docs at https://docs.github.com/en/rest and https://docs.rs/serde",
    "see https://en.wikipedia.org/wiki/IPv6 and https://pypi.org/project/x/1.2.3.4/",
    "pkg = 1.2.3.4 and pkg == 1.2.3.5 and pkg>=1.2.3.6",
    "Release notes for 1.2.3.4: fixes the leak.",
    '@Override public String toString() { return "x"; }',
    "/** @param x the value @returns the sum */",
    "@media (max-width: 600px) { .a { @apply p-4; } }",
    "DATABASE_URL=postgres://app@localhost:5432/db",
    "redis://localhost:6379/0 and http://127.0.0.1:8000/docs",
    "ssh deploy@localhost and git@github.com:o/r.git",
    "Deployed v1.2.3.4/linux to staging",
    "Upgrade to 1.2.3.4: fixes the leak",
    "route fd00::1 via fe80::1",
    "curl http://[::1]:8000/health",
    "connect from [::ffff:127.0.0.1]:51234",
)
"""Ordinary lines that must be written byte-identical in every carrier position."""


def carrier_cells() -> list[Cell]:
    """Every realistic carrier with every host class in its slot, plus the ordinary lines."""
    out: list[Cell] = []
    for name, template, lead_cls, follow_cls in CARRIERS:
        for hname, kind in CARRIER_HOSTS.items():
            host = KINDS[kind][0]
            text = template.format(host=host, ns=NS, repo=REPO)
            category, markers, note, prefix = expect(kind, lead_cls, follow_cls)
            if kind == "ipv6_doc" and lead_cls == "none":
                continue
            if "{ns}" in template and category == PLANTED and follow_cls == "segs":
                markers = tuple(dict.fromkeys((*markers, NS, REPO)))
            if "{ns}" in template and category == PLANTED and follow_cls == "seg":
                markers = tuple(m for m in markers if m != NS)
            if (
                hname == "ipv4_public"
                and lead_cls == "none"
                and follow_cls in ("seg", "segs", "port")
            ):
                category, prefix = REFUSED, "public address"
            out.append(
                Cell(
                    id=f"carrier:{name}:{hname}",
                    rule="carriers",
                    text=text,
                    category=category,
                    markers=markers,
                    note=note,
                    prefix=prefix,
                    remote=KNOWN_REMOTE if kind == "known_host" else Sample.remote,
                    position="stdout_text"
                    if name.startswith(("journald", "k8s_dial", "uvicorn"))
                    else "body",
                )
            )
    for i, line in enumerate(ORDINARY_CODE):
        for position in ("body", "stdout_text", "diff_plus", "stderr"):
            cat = ORDINARY
            note = ""
            if "github.com:o/r" in line:
                cat, note = REWRITE, "github.com is a forge host, replaced everywhere"
            out.append(
                Cell(
                    id=f"ordinary:{i:02d}:{position}",
                    rule="carriers",
                    text=line,
                    category=cat,
                    note=note,
                    position=position,
                )
            )
    return out


ROUTES: list[tuple[str, str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("gitlab_group", "see {lead}{host}/{ns}/-/issues?label_name=bug now", "ns", ("ns",), ()),
    ("gitlab_group_proto_rel", "see //{host}/{ns}/-/epics/12 now", "ns", ("ns",), ()),
    ("gitlab_group_subgroup", "see {lead}{host}/{ns}/{sub}/-/epics/3 now", "ns", ("ns", "sub"), ()),
    (
        "gitlab_groups_nested",
        "see {lead}{host}/groups/{ns}/{sub}/-/epics now",
        "ns",
        ("ns", "sub"),
        (),
    ),
    (
        "gitlab_groups_bare",
        "see {lead}{host}/groups/{ns} and the groups page",
        "ns",
        ("ns",),
        ("groups",),
    ),
    ("gitlab_user_snippets", "see {lead}{host}/users/{ns}/-/snippets now", "ns", ("ns",), ()),
    (
        "gitlab_project_route",
        "see {lead}{host}/{ns}/{repo}/-/merge_requests/1 now",
        "ns",
        ("ns", "repo"),
        (),
    ),
    ("gitlab_jobs_route", "see {lead}{host}/-/jobs/5 and the jobs page", "ns", (), ("jobs",)),
    (
        "gitlab_api_group",
        "see {lead}{host}/api/v4/groups/{ns}/projects now",
        "ns",
        ("ns",),
        ("projects",),
    ),
    (
        "github_orgs",
        "see {lead}{host}/orgs/{ns}/repositories and the repositories tab",
        "ns",
        ("ns",),
        ("repositories",),
    ),
    ("github_users", "see {lead}{host}/users/{ns}/repos now", "ns", ("ns",), ("repos",)),
    ("github_repo_route", "see {lead}{host}/{ns}/{repo}/pull/1 now", "ns", ("ns", "repo"), ()),
    (
        "github_api_repos",
        "see {lead}{host}/api/v3/repos/{ns}/{repo}/pulls now",
        "ns",
        ("ns", "repo"),
        ("pulls",),
    ),
    (
        "gitea_org",
        "see {lead}{host}/org/{ns}/dashboard and the dashboard",
        "third_party",
        ("ns",),
        ("dashboard",),
    ),
    (
        "gitea_api_orgs",
        "see {lead}{host}/api/v1/orgs/{ns}/repos now",
        "third_party",
        ("ns",),
        ("repos",),
    ),
    (
        "gitea_api_repos",
        "see {lead}{host}/api/v1/repos/{ns}/{repo}/pulls now",
        "third_party",
        ("ns", "repo"),
        ("pulls",),
    ),
    (
        "bitbucket_repo",
        "see {lead}{host}/{ns}/{repo}/pull-requests/1 now",
        "third_party",
        ("ns", "repo"),
        (),
    ),
    (
        "gitlab_web_ide_edit",
        "open {lead}{host}/-/ide/project/{ns}/{repo}/edit/main/-/src/app.py in the IDE",
        "ns",
        ("ns", "repo"),
        ("ide", "edit", "main", "src", "app.py"),
    ),
    (
        "gitlab_web_ide_subgroup_blob",
        "open {lead}{host}/-/ide/project/{ns}/{sub}/{repo}/blob/main/-/README.md now",
        "ns",
        ("ns", "sub", "repo"),
        ("ide", "blob", "main", "README.md"),
    ),
    (
        "gitlab_web_ide_tree",
        "open {lead}{host}/-/ide/project/{ns}/{repo}/tree/main/-/src/ now",
        "ns",
        ("ns", "repo"),
        ("ide", "tree", "main", "src"),
    ),
    (
        "gitlab_web_ide_merge_request",
        "open {lead}{host}/-/ide/project/{ns}/{repo}/merge_requests/3 now",
        "ns",
        ("ns", "repo"),
        ("ide", "merge_requests"),
    ),
    (
        "gitlab_web_ide_bare",
        "open {lead}{host}/-/ide/project/{ns}/{repo} now",
        "ns",
        ("ns", "repo"),
        ("ide",),
    ),
    (
        "bitbucket_server_personal_repo",
        "see {lead}{host}/users/{ns}/repos/{repo}/browse now",
        "third_party",
        ("ns", "repo"),
        ("users", "repos", "browse"),
    ),
    (
        "bitbucket_server_project_repo",
        "see {lead}{host}/projects/{ns}/repos/{repo}/pull-requests/1/overview now",
        "third_party",
        ("ns", "repo"),
        ("projects", "repos", "pull-requests", "overview"),
    ),
    (
        "github_org_team",
        "see {lead}{host}/orgs/{ns}/teams/{sub}/members now",
        "ns",
        ("ns", "sub"),
        ("orgs", "teams", "members"),
    ),
    (
        "github_api_org_team",
        "see {lead}{host}/api/v3/orgs/{ns}/teams/{sub} now",
        "ns",
        ("ns", "sub"),
        ("orgs", "teams"),
    ),
    (
        "gitlab_npm_instance_scope",
        "resolved {lead}{host}/api/v4/packages/npm/@{ns}/kvnpkg/-/kvnpkg-1.0.0.tgz now",
        "ns",
        (),
        ("packages", "npm", "kvnpkg-1.0.0.tgz", f"@{NS}"),
    ),
    (
        "gitlab_npm_project_scope_file_name",
        "resolved {lead}{host}/api/v4/projects/123/packages/npm/@{ns}/kvnpkg/-/@{ns}/kvnpkg-1.0.0.tgz now",
        "ns",
        (),
        ("projects", "packages", "kvnpkg", f"@{NS}"),
    ),
    (
        "gitlab_npm_group_scope",
        "resolved {lead}{host}/api/v4/groups/{sub}/-/packages/npm/@{ns}/kvnpkg/-/kvnpkg-1.0.0.tgz now",
        "ns",
        ("sub",),
        ("groups", "packages", "kvnpkg", f"@{NS}"),
    ),
    (
        "gitlab_npm_metadata_scope",
        "GET {lead}{host}/api/v4/packages/npm/@{ns}%2Fkvnpkg now",
        "ns",
        (),
        ("packages", "kvnpkg", f"@{NS}"),
    ),
    (
        "gitlab_npm_dist_tags_scope",
        "PUT {lead}{host}/api/v4/projects/123/packages/npm/-/package/@{ns}%2Fkvnpkg/dist-tags/latest now",
        "ns",
        (),
        ("package", "dist-tags", "kvnpkg", f"@{NS}"),
    ),
    (
        "gitlab_npm_forwarded_types",
        "resolved {lead}{host}/api/v4/projects/123/packages/npm/@types/node/-/node-20.11.0.tgz now",
        "ns",
        (),
        ("@types/node", "node-20.11.0.tgz"),
    ),
    (
        "gitlab_npm_forwarded_babel",
        "GET {lead}{host}/api/v4/groups/{sub}/-/packages/npm/@babel%2Fcore now",
        "ns",
        ("sub",),
        ("@babel%2Fcore",),
    ),
]
"""Forge route grammars (0.21, B1; 0.22: the Web IDE and the owner-root identity slots): name, template, where it is met (``ns``: any host, the recorded instance included; ``third_party``: a Gitea or Bitbucket host, never the instance, which is GitHub or GitLab), the identity segments, the route words that must stay."""

RESERVED_WORDS: dict[str, tuple[str, ...]] = {
    "gitlab": (
        "uploads",
        "help",
        "explore",
        "admin",
        "dashboard",
        "snippets",
        "search",
        "public",
        "s",
        "files",
        "assets",
        "import",
        "oauth",
        "jwt",
        ".well-known",
        "-",
    ),
    "github": (
        "codespaces",
        "collections",
        "join",
        "login",
        "logout",
        "marketplace",
        "notifications",
        "organizations",
        "pricing",
        "sessions",
        "settings",
        "topics",
        "trending",
        "explore",
        "search",
        "dashboard",
    ),
    "gitea": ("attachments", "avatars", "milestones", "repo", "explore", "notifications"),
    "bitbucket": ("account", "plugins", "dashboard", "snippets"),
}
"""The top-level words each forge reserves for its own pages (0.22): read from the forges' routing, not from the code under test."""

RESERVED_SHAPES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("pages", "see {lead}{host}/{word}/pages/index and the {word} page", ("pages", "index")),
    ("dash", "img {lead}{host}/{word}/-/system/zqfile.png now", ("system", "zqfile.png")),
)
"""A reserved word followed by two segments, and by a GitLab ``/-/`` route: the word and what follows are written, nothing is learnt."""

TARBALLS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "npm_tarball",
        'resolved "{lead}{host}/lodash/-/lodash-4.17.21.tgz#abc" and lodash is fine',
        ("lodash",),
        "body",
    ),
    (
        "npm_scoped_tarball",
        "resolved {lead}{host}/@babel/core/-/core-7.24.0.tgz now",
        ("@babel", "core-7.24.0.tgz"),
        "body",
    ),
    (
        "nexus_tarball",
        "resolved {lead}{host}/repository/npm-all/lodash/-/lodash-4.17.21.tgz now",
        ("repository", "npm-all", "lodash"),
        "body",
    ),
    (
        "npm_prerelease_tarball",
        "resolved {lead}{host}/lodash/-/lodash-4.17.21-beta.1.tgz now",
        ("lodash-4.17.21-beta.1.tgz",),
        "body",
    ),
    (
        "yarn_lock_diff",
        '  resolved "{lead}{host}/lodash/-/lodash-4.17.21.tgz#abc"',
        ("lodash",),
        "diff_plus",
    ),
)
"""The npm tarball grammar ``<pkg>/-/<pkg>-<version>.tgz`` (0.22): plain, scoped, under a Nexus prefix, a prerelease, in a lock-file diff; the package is never an identity."""

ROUTE_HOSTS: dict[str, str] = {
    "host3": "host3",
    "known_host": "known_host",
    "ipv4_private": "ipv4_private",
    "localhost": "localhost",
    "public_host": "public_host",
}

REGISTRY_HOSTS: dict[str, tuple[str, str]] = {
    "npmjs": ("registry.npmjs.org", "public"),
    "yarnpkg": ("registry.yarnpkg.com", "public"),
    "npmmirror": ("registry.npmmirror.com", "public"),
    "private_npm": ("npm.kvncorp.net", "host3"),
    "nexus": ("nexus.kvncorp.net", "host3"),
}
"""The registry hosts of the tarball cells (0.22): the three public registries of the allowlist, a private one, a Nexus."""


def _route_cell(
    name: str,
    hname: str,
    host: str,
    cls: str,
    lead_name: str,
    text: str,
    identity: tuple[str, ...],
    kept: tuple[str, ...],
    position: str = "body",
) -> Cell:
    """One route cell: on a placeholder or public host nothing is read, elsewhere the host and the identity are values."""
    values = {"ns": NS, "sub": "kvnsub", "repo": REPO}
    markers = tuple(values[k] for k in identity)
    if cls in ("placeholder", "public"):
        category, note = (RESIDUAL, NG4_12) if markers else (ORDINARY, "")
        markers = ()
    else:
        category, note = PLANTED, "the identity before the route is a value"
        markers = (host, *markers)
    return Cell(
        id=f"route:{name}:{hname}:{lead_name}",
        rule="routes",
        text=text,
        category=category,
        markers=markers,
        note=note,
        position=position,
        remote=KNOWN_REMOTE if cls == "known" else Sample.remote,
        extra={"kept": kept},
    )


def route_cells() -> list[Cell]:
    """Every forge route grammar with every host class: the identity before the route is learnt, the route words are not.

    0.22 adds, on the same host classes, every forge's reserved words in
    two shapes (nothing is learnt, the word stays) and the npm tarball
    grammar on the route hosts and on five registry hosts; 0.23 adds the
    five shapes of GitLab's npm registry, and 0.24 two forwarded public
    scopes: what follows ``packages/npm/`` names no one, its ``@<scope>``
    included (NG4-15), while what stands before it reads as a route.
    """
    out: list[Cell] = []
    leads = (("https", "https://"), ("http", "http://"))
    for name, template, where, identity, kept in ROUTES:
        for hname, kind in ROUTE_HOSTS.items():
            host, cls = KINDS[kind]
            if where == "third_party" and cls == "known":
                continue
            for lead_name, lead in leads:
                if "{lead}" not in template and lead_name == "http":
                    continue
                text = template.format(lead=lead, host=host, ns=NS, sub="kvnsub", repo=REPO)
                out.append(_route_cell(name, hname, host, cls, lead_name, text, identity, kept))
    for forge, words in RESERVED_WORDS.items():
        for word in words:
            for shape, template, rest in RESERVED_SHAPES:
                for hname, kind in ROUTE_HOSTS.items():
                    host, cls = KINDS[kind]
                    for lead_name, lead in leads:
                        text = template.format(lead=lead, host=host, word=word)
                        name = f"reserved:{forge}:{word}:{shape}"
                        out.append(
                            _route_cell(name, hname, host, cls, lead_name, text, (), (word, *rest))
                        )
    hosts = {h: KINDS[k] for h, k in ROUTE_HOSTS.items()} | REGISTRY_HOSTS
    for name, template, kept, position in TARBALLS:
        for hname, (host, cls) in hosts.items():
            for lead_name, lead in leads:
                text = template.format(lead=lead, host=host)
                out.append(_route_cell(name, hname, host, cls, lead_name, text, (), kept, position))
    return out


NEEDLE_MIN = 4
"""D7: the shortest value a structured source may hold; a shorter one is a configuration error."""


NESTED_SHAPES: tuple[tuple[str, str], ...] = (
    ("dash", "see https://{host}/{word}/kvnsub/zqmainrepo/-/merge_requests/1 and kvnsub"),
    ("page", "see https://{host}/{word}/kvnsub/zqmainrepo and https://{host}/{word}/kvnsub now"),
    ("scp", "clone git@{host}:{word}/kvnsub/zqmainrepo.git now"),
)
"""A nested remote under a reserved root word met in text (0.24, B1-a): a route after ``/-/``, the project and group pages, the clone address."""

ARGV_SHAPES: tuple[tuple[str, str], ...] = (
    ("github", "repos/{word}/kvnbilling/pulls/5/comments"),
    ("gitlab", "projects/{word}%2Fkvnbilling/merge_requests/1/notes"),
    ("gitlab_nested", "api/v4/projects/{word}%2Fkvnsub%2Fkvnbilling/issues"),
)
"""An argv API path naming a project whose namespace is a reserved word (0.24, B1-b): a structured source, read by construction."""


def remote_cells() -> list[Cell]:
    """A recorded remote or an argv API path whose namespace is a reserved word (0.23, B1; 0.24, B1-a and B1-b): both name a repository by construction.

    The reserved-word rule is a URL path's; the namespace and repository of
    a remote, flat or nested, and of an argv ``repos/<o>/<r>`` or
    ``projects/<ns>%2F<r>`` path are values on every word of the sweep's own
    table that an account may carry, and one shorter than
    :data:`NEEDLE_MIN` is the D7 configuration error, refused by name.
    """
    out: list[Cell] = []
    words = sorted({w for forge in RESERVED_WORDS.values() for w in forge})
    for word in words:
        if not word[0].isalnum() or not all(c.isalnum() or c == "-" for c in word):
            continue
        short = len(word) < NEEDLE_MIN
        for shape, template in NESTED_SHAPES:
            out.append(
                Cell(
                    id=f"remote:nested:{word}:{shape}",
                    rule="routes",
                    text=template.format(host=KNOWN_HOST, word=word),
                    category=REFUSED if short else PLANTED,
                    markers=() if short else (KNOWN_HOST, "kvnsub", "zqmainrepo"),
                    note="a short namespace is a configuration error (D7)"
                    if short
                    else "a nested remote names a repository by construction",
                    prefix="needle segment" if short else "",
                    remote=f"git@{KNOWN_HOST}:{word}/kvnsub/zqmainrepo.git",
                )
            )
        for shape, template in ARGV_SHAPES:
            out.append(
                Cell(
                    id=f"argv:{shape}:{word}",
                    rule="routes",
                    text=template.format(word=word),
                    category=REFUSED if short else PLANTED,
                    markers=() if short else ("kvnbilling", f"/{word}/", f"{word}%2f"),
                    note="a short namespace is a configuration error (D7)"
                    if short
                    else "an argv API path names a repository by construction",
                    prefix="needle segment" if short else "",
                    position="argv",
                )
            )
        out.append(
            Cell(
                id=f"remote:{word}",
                rule="routes",
                text=f"see https://{KNOWN_HOST}/{word}/zqmainrepo/pull/5 and zqmainrepo again",
                category=REFUSED if short else PLANTED,
                markers=() if short else (KNOWN_HOST, "zqmainrepo"),
                note="a short namespace is a configuration error (D7)"
                if short
                else "the remote names a repository by construction",
                prefix="needle segment" if short else "",
                remote=f"git@{KNOWN_HOST}:{word}/zqmainrepo.git",
            )
        )
    return out


def language_word_cells() -> list[Cell]:
    """Every language word after ``@``: mention-only it is written (NG4-13), a participant's login it is replaced."""
    out: list[Cell] = []
    for word in sorted(corpus.rec_mod.LANGUAGE_AT_WORDS):
        out.append(
            Cell(
                id=f"logins:mention_only:{word}",
                rule="logins",
                text=f"cc @{word} please review",
                category=RESIDUAL_UNCHANGED,
                note=NG4_13,
            )
        )
        if corpus.rec_mod.CLASS_WORD_RE.match(word):
            continue
        short = len(word) < corpus.rec_mod.NEEDLE_MIN_LENGTH
        out.append(
            Cell(
                id=f"logins:participant:{word}",
                rule="logins",
                text=f"cc @{word} please review, {word}",
                category=REFUSED if short else PLANTED,
                markers=() if short else (f"@{word}",),
                note="a short login is a configuration error (D7)"
                if short
                else "a participant login is a known value",
                prefix="needle login" if short else "",
                extra={"login": word},
            )
        )
    return out


def nesting_cells() -> list[Cell]:
    """Nesting depth (0.21, B3): shallow values are ordinary, deep ones are written or refused, never a crash."""
    out: list[Cell] = []
    for depth in (1, 10, 100, 990, 2000, 20000):
        for shape, text in (
            ("open_brackets", "[" * depth),
            ("lists", "[" * depth + "1" + "]" * depth),
            ("dicts", '{"a":' * depth + "1" + "}" * depth),
        ):
            for position in ("body", "stdout_text", "stderr"):
                if depth <= 100:
                    category = (
                        REWRITE if shape == "dicts" and position == "stdout_text" else ORDINARY
                    )
                    prefix = ""
                else:
                    category, prefix = BOUNDED, "nesting too deep"
                out.append(
                    Cell(
                        id=f"nesting:{shape}:{depth}:{position}",
                        rule="nesting",
                        text=text,
                        category=category,
                        prefix=prefix,
                        position=position,
                    )
                )
    return out


def cells() -> list[Cell]:
    """The whole sweep, in a fixed order, ids unique."""
    out = [
        *grid_cells(),
        *version_context_cells(),
        *encoded_cells(),
        *position_cells(),
        *known_value_cells(),
        *mailbox_cells(),
        *mention_cells(),
        *carrier_cells(),
        *route_cells(),
        *remote_cells(),
        *language_word_cells(),
        *nesting_cells(),
    ]
    ids = [c.id for c in out]
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1][:5]
    return out


def sample(cell: Cell) -> Sample:
    """The row input that carries the cell's text at its position."""
    text, position = cell.text, cell.position
    base: dict[str, Any] = {"remote": cell.remote}
    if "login" in cell.extra:
        base["login"] = cell.extra["login"]
    if position == "body":
        return Sample(body=text, **base)
    if position == "title":
        return Sample(title=text, body="x", **base)
    if position == "stderr":
        return Sample(stderr=text, **base)
    if position == "stdin":
        return Sample(stdin=text, **base)
    if position == "stdout_text":
        return Sample(raw_text_stdout=text, **base)
    if position.startswith("diff_"):
        mark = {"diff_plus": "+", "diff_minus": "-", "diff_context": " "}[position]
        diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n" + "".join(
            f"{mark}{line}\n" for line in text.split("\n")
        )
        return Sample(raw_text_stdout=diff, **base)
    if position == "args":
        return Sample(top_args=(42, text), **base)
    if position == "kwargs":
        return Sample(top_kwargs={"note": text}, **base)
    if position == "json_value":
        return Sample(extra_item={"message": text}, **base)
    if position == "json_list":
        return Sample(extra_item={"notes": [text, "x"]}, **base)
    if position == "json_key":
        return Sample(top_kwargs={text: 1}, **base)
    if position == "argv":
        return Sample(api_path=text, body="ok", **base)
    if position == "ci_log":
        return Sample(
            raw_text_stdout=f"[2026-11-22T10:00:00Z] step 3/7\n{text}\nDone in 2.1s\n", **base
        )
    raise AssertionError(position)


def _strings(node: object) -> list[str]:
    """Every string of a written fixture, JSON stdout decoded, keys included."""
    if isinstance(node, str):
        decoded = (
            corpus.rec_mod.decode_json_values(node) if node.lstrip()[:1] in ("{", "[") else None
        )
        if decoded is None:
            return [node]
        return [node, *(s for v in decoded for s in _strings(v))]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in (str(k), *_strings(v))]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


@dataclass(frozen=True)
class Outcome:
    """What one cell did: the status, the refusal message or the written strings, and the verdict."""

    status: str
    message: str
    verdict: str
    detail: str = ""


def run_cell(cell: Cell, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Outcome:
    """Run one cell live, then offline, and judge it against its category."""
    row = corpus.Row(id=cell.id, source="sweep", inp=sample(cell), outcome=corpus.Written(text=""))
    try:
        status, msg, fixture = corpus._record_live(row, monkeypatch, tmp_path)
    except Exception as exc:
        return Outcome("CRASH", type(exc).__name__, "CRASH", type(exc).__name__)
    if status not in ("WRITTEN", "REFUSED"):
        return Outcome(status, msg[:80], "CRASH", status)
    strings = _strings(fixture) if fixture is not None else []
    blob = "\n".join(strings).casefold()
    survivors = [m for m in cell.markers if m.casefold() in blob]
    present = any(cell.text in s for s in strings)
    offline: list[str] = []
    if fixture is not None:
        offline = corpus._judge_offline(row, fixture)
    elif cell.prefix:
        refused = replace(row, outcome=corpus.Refused(prefix=cell.prefix))
        offline = corpus._judge_offline_refusal(refused, msg)
    verdict, detail = "ok", ""
    if cell.category == ORDINARY:
        if status == "REFUSED":
            verdict, detail = "OVER-REFUSAL", msg[:60]
        elif not present:
            verdict, detail = "CORRUPTION", ""
    elif cell.category == REWRITE:
        if status == "REFUSED":
            verdict, detail = "OVER-REFUSAL", msg[:60]
    elif cell.category == PLANTED:
        if status == "WRITTEN" and survivors:
            verdict, detail = "LEAK", ",".join(survivors)
    elif cell.category == RESIDUAL_UNCHANGED:
        if status == "REFUSED":
            verdict, detail = "OVER-REFUSAL", msg[:60]
        elif not present:
            verdict, detail = "CORRUPTION", ""
    elif cell.category == REFUSED:
        if status != "REFUSED":
            verdict, detail = "LEAK", "written"
        elif not msg.startswith(cell.prefix):
            verdict, detail = "WRONG-REASON", msg[:60]
    elif cell.category == BOUNDED and status == "REFUSED" and not msg.startswith(cell.prefix):
        verdict, detail = "WRONG-REASON", msg[:60]
    kept = [w for w in cell.extra.get("kept", ()) if w.casefold() not in blob]
    if verdict == "ok" and status == "WRITTEN" and kept:
        verdict, detail = "CORRUPTION", "route word replaced"
    if verdict == "ok" and offline:
        verdict, detail = "OFFLINE", offline[0][:80]
    return Outcome(status, msg[:80], verdict, detail)


def in_sample(cell_id: str, modulus: int = 5) -> bool:
    """A deterministic fifth of the sweep, by a stable hash of the id."""
    return zlib.crc32(cell_id.encode()) % modulus == 0


def test_the_sweep_states_every_rule_and_names_at_least_two_thousand_cells() -> None:
    """AC8 (0.20): the sweep covers every rule with a stated expectation; its size is pinned from below."""
    all_cells = cells()
    assert len(all_cells) >= 2000, len(all_cells)
    rules = {c.rule for c in all_cells}
    assert rules == {
        "hosts",
        "ipv4",
        "ipv6",
        "decoded",
        "positions",
        "known",
        "mailbox",
        "mentions",
        "carriers",
        "routes",
        "logins",
        "nesting",
    }
    assert all(
        c.category in (ORDINARY, REWRITE, PLANTED, RESIDUAL, RESIDUAL_UNCHANGED, REFUSED, BOUNDED)
        for c in all_cells
    )
    assert all(c.prefix for c in all_cells if c.category == BOUNDED)
    assert all(c.markers for c in all_cells if c.category == PLANTED)
    assert all(c.prefix for c in all_cells if c.category == REFUSED)
    assert sum(in_sample(c.id) for c in all_cells) >= 500


def test_a_deterministic_sample_of_the_sweep_meets_the_owners_goal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC8 (0.20): no crash, no leak outside the documented residuals, no corruption or over-refusal of ordinary text."""
    logging.disable(logging.CRITICAL)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    failures: list[str] = []
    count = 0
    for cell in cells():
        if not in_sample(cell.id):
            continue
        count += 1
        outcome = run_cell(cell, monkeypatch, tmp_path)
        if outcome.verdict != "ok":
            failures.append(f"{cell.id}: {outcome.verdict} {outcome.detail}")
    assert count >= 500
    assert failures == [], f"{len(failures)} of {count} cells: " + json.dumps(failures[:20])
