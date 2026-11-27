#!/usr/bin/env python3
"""Record one forge conformance fixture against a real PR / MR (read-only).

SP-UNIVERSAL-FORGE A1: when ``gh`` or ``glab`` changes, re-record the
fixtures under ``tests/fixtures/forge/<forge>/`` against a real change
request so the conformance suite keeps pinning what the CLI actually
prints. The script runs ONE protocol method on the forge selected for
``--cwd`` and writes the subprocess exchanges it made plus its
normalised result::

    scripts/record_forge_fixture.py --forge gitlab --cwd ../some-checkout \\
        --case pull_request --method pull_request --args '[283]'

Write methods are refused: a recording must never post, merge or open
anything. Their fixtures are written by hand from the forge's documented
payloads and say so in their ``source`` field.

Every JSON answer is redacted at record time: only the keys the
forges read (:data:`repoach.forge.fixture_reading.RECORDED_FIELDS`) are kept,
and a fixture in which a token- or email-shaped value survives is refused.

SP-RECORDER-REDACTS-BY-CLASS: personal keys, a user object's ``name``
and URLs are dropped, every URL and scp-style remote the convenience layer
recognises gets the placeholder host, namespace and repository
(:func:`repoach.forge.recording.redact_url`,
``forge.example.com/org/project``), and every login becomes its class
word in stdout, argv and the method's arguments: the bot ``bot``, the
account an ``is_maintainer`` call asks about ``maintainer``, the change
request's author ``author``, any other ``[bot]`` login ``bot-<n>``, a
GitLab project bot ``service`` and any other login ``author-<n>``; a
login known only from an ``@<handle>`` in free text is ``author-<n>`` in
that form only, and a decorator or a dotted name is no handle; the
:data:`repoach.forge.fixture_reading.NON_PERSON_LOGINS` are kept. Every user
id becomes a synthetic one in ``1001..1999``
(:class:`repoach.forge.recording.UserIdMap`), declared under
``synthetic_user_ids``.

Every private value the recording holds in a structured field, and the
hosts the oracle reads in any string, is a known value
(:func:`repoach.forge.recording.known_values`); each is replaced in every
string by its class token (:func:`repoach.forge.recording.replace_known`),
whatever shape it stands in. The result is then recomputed by replaying
the redacted exchanges, so the fixture is what the readers return from it.
The whole fixture, its ``source`` included, is judged last: a login left
unclassified, a home path, an entry of the maintainer's deny-list or
anything the closure oracle (:func:`repoach.forge.closure.judge`)
refuses (a known value, a host that is neither a placeholder nor public,
a public address, a mailbox that is neither a placeholder nor a
documentation literal) exits 2 naming a reason, a kind, an ordinal and
the key path, never the value, and writes nothing. ``source`` names the
forge and the date, never the checkout's folder. Logs go to stderr;
stdout is the fixture's path or nothing.

``--trim N`` keeps only the first ``N`` items of every JSON array in
recorded stdout (and of the recorded result), for listings too large to
commit; the fixture records that it was trimmed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from repoach.core.denylist import DenylistError
from repoach.core.logging import route_default_logs_to_stderr
from repoach.forge.fixture_judge import UnclassifiedValueError
from repoach.forge.fixture_pipeline import FixtureWriteError, OriginRemoteError, record_live
from repoach.forge.protocol import ForgeError
from repoach.forge.recording import NeedleError, SyntheticIdsExhaustedError

__all__ = ["FIXTURE_ROOT", "main"]

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "forge"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: one line on stdout, the fixture's path, or nothing and exit 2 on a refusal.

    Args:
        argv: The command line after the program name; ``sys.argv[1:]`` when ``None``.

    Returns:
        ``0`` when the fixture is written, ``2`` on a refusal (the reason on stderr).
    """
    route_default_logs_to_stderr()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--forge", required=True, choices=["github", "gitlab"])
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--case", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--args", default="[]", help="JSON list of positional arguments")
    parser.add_argument("--kwargs", default="{}", help="JSON object of keyword arguments")
    parser.add_argument("--trim", type=int, default=None)
    parser.add_argument("--out", type=Path, default=FIXTURE_ROOT)
    ns = parser.parse_args(argv)
    try:
        path = record_live(
            forge_name=ns.forge,
            cwd=ns.cwd,
            case=ns.case,
            method=ns.method,
            args=json.loads(ns.args),
            kwargs=json.loads(ns.kwargs),
            trim=ns.trim,
            out_root=ns.out,
        )
    except (
        UnclassifiedValueError,
        DenylistError,
        OriginRemoteError,
        FixtureWriteError,
        SyntheticIdsExhaustedError,
        NeedleError,
        ForgeError,
    ) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
