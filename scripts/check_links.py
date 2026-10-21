#!/usr/bin/env python3
"""Check every link of the given Markdown files: relative paths, anchors and ``https://`` URLs.

Shared by SP-COMPARISON-PAGE (``scripts/gen_comparison.py`` checks its own
output with it) and SP-README-PRODUCT-STATEMENT (``README.md``).

Usage::

    python scripts/check_links.py README.md docs/comparison.md

Links are read from Markdown inline links and images (``[text](target)``)
and from the ``href``, ``src`` and ``srcset`` attributes of inline HTML.
Fenced code blocks and inline code spans are skipped: a URL shown as an
example is not a link.

A relative target must exist on disk, relative to the file that names it;
a ``#fragment`` pointing into a Markdown file must match one of its
headings, slugged the way GitHub slugs them. A network target is fetched
with a plain ``GET``; an answer of 403 is retried once with a browser
``User-Agent`` (some vendor pages refuse non-browser agents), and a second
refusal is reported ``blocked``.

Exit codes:

* ``0`` every link resolves; the last line is ``links: N checked, 0 dead``.
* ``1`` at least one dead link, one ``<file>:<line> <url> <status>`` line each.
* ``2`` ``cannot say``: no network URL received any HTTP answer (DNS or
  network down, so a dead link cannot be told from a dead network), or a
  URL that resolved never answered. CI treats 2 as a retry, never as green.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_USER_AGENT = "repoach-link-check/1 (+https://github.com/repoachhq/repoach)"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
TIMEOUT_S = 20.0

EXIT_OK = 0
EXIT_DEAD = 1
EXIT_CANNOT_SAY = 2

_MD_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_HTML_ATTR = re.compile(r"\b(?:href|src|srcset)=\"([^\"]+)\"")
_INLINE_CODE = re.compile(r"`+[^`]*`+")
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SKIPPED_SCHEMES = ("mailto:", "tel:", "sms:", "data:")


@dataclass(frozen=True)
class Link:
    """One link occurrence.

    Attributes:
        file: The Markdown file that names the link.
        line: 1-based line number of the occurrence.
        target: The link target exactly as written.
    """

    file: Path
    line: int
    target: str

    @property
    def is_network(self) -> bool:
        """Whether the target is fetched over HTTP rather than read from disk."""
        return self.target.startswith(("http://", "https://"))


@dataclass(frozen=True)
class Probe:
    """What one HTTP fetch observed.

    Attributes:
        status: The HTTP status, or ``None`` when no HTTP answer arrived.
        resolved: Whether the host name resolved; ``False`` means the URL
            names a host that does not exist (a dead link, not a dead network).
    """

    status: int | None
    resolved: bool = True


Fetch = Callable[[str, str], Probe]


@dataclass(frozen=True)
class LinkResult:
    """The verdict on one link occurrence.

    Attributes:
        link: The occurrence judged.
        problem: ``None`` when the link resolves; otherwise the status word
            printed after the URL (an HTTP code, ``blocked``, ``unresolved``,
            ``missing``, ``no-anchor``, or ``unanswered`` for cannot-say).
    """

    link: Link
    problem: str | None

    @property
    def cannot_say(self) -> bool:
        """Whether the verdict is unknown rather than dead."""
        return self.problem == "unanswered"


@dataclass(frozen=True)
class Report:
    """All verdicts for one run.

    Attributes:
        results: One verdict per link occurrence, in file order.
        network_answered: Whether at least one network URL got an HTTP answer;
            ``False`` with network links present means the run cannot say.
    """

    results: tuple[LinkResult, ...]
    network_answered: bool

    @property
    def dead(self) -> tuple[LinkResult, ...]:
        """The verdicts that name a dead link."""
        return tuple(r for r in self.results if r.problem and not r.cannot_say)

    @property
    def exit_code(self) -> int:
        """The process exit code this report maps to."""
        has_network = any(r.link.is_network for r in self.results)
        if has_network and not self.network_answered:
            return EXIT_CANNOT_SAY
        if self.dead:
            return EXIT_DEAD
        if any(r.cannot_say for r in self.results):
            return EXIT_CANNOT_SAY
        return EXIT_OK


def urllib_fetch(url: str, user_agent: str) -> Probe:
    """Fetch *url* with a plain ``GET`` and report what answered.

    Args:
        url: The absolute URL.
        user_agent: The ``User-Agent`` header to send.

    Returns:
        The HTTP status after redirects, or a status-less probe saying
        whether the host resolved.
    """
    request = urllib.request.Request(url, headers={"User-Agent": user_agent}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return Probe(status=int(response.status))
    except urllib.error.HTTPError as exc:
        return Probe(status=int(exc.code))
    except urllib.error.URLError as exc:
        return Probe(status=None, resolved=not isinstance(exc.reason, socket.gaierror))
    except (TimeoutError, OSError):
        return Probe(status=None)


def github_slug(heading: str) -> str:
    """Slug a heading the way GitHub builds its anchors.

    Args:
        heading: The heading text, without the leading ``#`` marks.

    Returns:
        The lowercase anchor, punctuation dropped and spaces turned into ``-``.
    """
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    """Collect the heading anchors of a Markdown file, duplicates suffixed ``-1``, ``-2``.

    Args:
        path: The Markdown file.

    Returns:
        Every anchor a ``#fragment`` may point to.
    """
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADING.match(line)
        if match is None:
            continue
        slug = github_slug(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def extract_links(path: Path, text: str | None = None) -> list[Link]:
    """Read every link occurrence of a Markdown file, code excluded.

    Args:
        path: The Markdown file; relative targets resolve against its directory.
        text: The content to read instead of the file's, for a page that is
            rendered but not yet written.

    Returns:
        The occurrences in file order.
    """
    links: list[Link] = []
    in_fence = False
    content = path.read_text(encoding="utf-8") if text is None else text
    for number, line in enumerate(content.splitlines(), start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        visible = _INLINE_CODE.sub("", line)
        targets = [m.group(1) for m in _MD_LINK.finditer(visible)]
        for match in _HTML_ATTR.finditer(visible):
            targets.append(match.group(1).split(",")[0].split()[0])
        for target in targets:
            if not target.startswith(_SKIPPED_SCHEMES):
                links.append(Link(file=path, line=number, target=target))
    return links


def _local_problem(link: Link) -> str | None:
    """Judge a relative or ``#fragment`` target against the file system."""
    raw, _, fragment = link.target.partition("#")
    raw = raw.split("?")[0]
    target = link.file if not raw else (link.file.parent / raw)
    if not target.exists():
        return "missing"
    is_markdown = target.is_file() and target.suffix.lower() == ".md"
    if fragment and is_markdown and fragment.lower() not in anchors_of(target):
        return "no-anchor"
    return None


def _network_problem(probe: Probe) -> str | None:
    """Map one probe to a problem word, ``None`` when it answered below 400."""
    if probe.status is None:
        return "unresolved" if not probe.resolved else "unanswered"
    if probe.status >= 400:
        return str(probe.status)
    return None


def probe_url(url: str, fetch: Fetch) -> Probe:
    """Fetch *url*, retrying a 403 once with a browser ``User-Agent``.

    Args:
        url: The absolute URL.
        fetch: The transport.

    Returns:
        The last probe; a 403 that survives the retry is returned as is and
        reported ``blocked`` by :func:`url_problem`.
    """
    probe = fetch(url, DEFAULT_USER_AGENT)
    if probe.status == 403:
        probe = fetch(url, BROWSER_USER_AGENT)
    return probe


def url_problem(probe: Probe) -> str | None:
    """Name what is wrong with a network probe.

    Args:
        probe: The probe returned by :func:`probe_url`.

    Returns:
        ``None`` when the URL answered below 400, ``blocked`` for a 403 that
        survived the browser retry, else the status word.
    """
    if probe.status == 403:
        return "blocked"
    return _network_problem(probe)


def check(links: Iterable[Link], fetch: Fetch = urllib_fetch) -> Report:
    """Judge every link, fetching each distinct network URL once.

    Args:
        links: The occurrences to judge.
        fetch: The HTTP transport; tests pass a fake one.

    Returns:
        The report, one verdict per occurrence.
    """
    probes: dict[str, Probe] = {}
    results: list[LinkResult] = []
    for link in links:
        if link.is_network:
            if link.target not in probes:
                probes[link.target] = probe_url(link.target, fetch)
            results.append(LinkResult(link=link, problem=url_problem(probes[link.target])))
        else:
            results.append(LinkResult(link=link, problem=_local_problem(link)))
    answered = any(p.status is not None for p in probes.values())
    return Report(results=tuple(results), network_answered=answered)


def render(report: Report) -> list[str]:
    """Render the report as the lines the CLI prints.

    Args:
        report: The report.

    Returns:
        One line per problem, then the summary line.
    """
    lines = [
        f"{r.link.file}:{r.link.line} {r.link.target} {r.problem}"
        for r in report.results
        if r.problem
    ]
    if report.exit_code == EXIT_CANNOT_SAY:
        lines.append(f"links: {len(report.results)} checked, cannot say (no HTTP answer)")
    else:
        lines.append(f"links: {len(report.results)} checked, {len(report.dead)} dead")
    return lines


def main(argv: Sequence[str] | None = None, fetch: Fetch = urllib_fetch) -> int:
    """Run the checker over the files named on the command line.

    Args:
        argv: The arguments, without the program name.
        fetch: The HTTP transport.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(description="Check the links of Markdown files.")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    links: list[Link] = []
    for path in args.files:
        if not path.is_file():
            print(f"{path}: no such file", file=sys.stderr)
            return EXIT_DEAD
        links.extend(extract_links(path))
    report = check(links, fetch)
    for line in render(report):
        print(line)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
