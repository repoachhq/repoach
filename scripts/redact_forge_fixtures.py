#!/usr/bin/env python3
"""Re-apply the recorder's class redaction to every recorded forge fixture (offline).

SP-RECORDER-REDACTS-BY-CLASS: ``scripts/record_forge_fixture.py`` writes
URLs with the placeholder host and namespace, drops personal keys and a
user object's ``name`` and URLs, classifies logins and synthesizes user
ids at record time. The fixtures recorded or written before it are
transformed by the same functions, deterministically and without any
network call::

    scripts/redact_forge_fixtures.py            # rewrite in place
    scripts/redact_forge_fixtures.py --check    # exit 1 when a file would change

This script is the command line only: the transform is
:func:`repoach.forge.fixture_pipeline.redact_corpus` and, for one fixture,
:func:`repoach.forge.fixture_pipeline.redact_fixture`, the pipeline module the
live recorder finishes through as well.

In a fixture, every exchange (an object holding ``stdout`` and
``returncode``) has its stdout redacted by
:func:`repoach.forge.recording.redact_payload` when it is JSON and its
URLs redacted when it is text, its stderr's and stdin's URLs redacted; the
top-level ``args``, ``kwargs`` and ``result`` have their URLs redacted.

Logins follow the real-account rule the recorder applies: every login
that is not a class word or one of
:data:`repoach.forge.fixture_reading.NON_PERSON_LOGINS` — a hand-written
placeholder such as ``alice`` or ``owner`` included, since each is a real
account — becomes the next free ``author-<n>`` (a ``[bot]`` login
``bot-<n>``, a GitLab project bot ``service``), under login keys, as whole
arguments, as path segments and as ``@<handle>`` mentions in free text; a
login known only from a mention is replaced in its ``@<handle>`` form only.

Every user id the fixture does not declare under
:data:`repoach.forge.fixture_reading.SYNTHETIC_IDS_KEY`, or declares outside
``1001..1999`` (:func:`repoach.forge.recording.trusted_declaration`),
becomes a synthetic one, from one sequence shared by the whole corpus and
keyed by ``(forge, id)`` (:class:`repoach.forge.recording.UserIdMap`), so
one account keeps one number across files and two forges' accounts never
share one; the sequence starts after the highest trusted number any
fixture declares, and a corpus that would need a number past the range is
exit 2 with nothing written. A recorded fixture's ``source`` names the forge, never the
checkout's folder. A repository id becomes a synthetic one in
``2001..2999``, declared under ``synthetic_project_ids``. Every fixture is
judged by the recorder's own judge
(:func:`repoach.forge.fixture_judge.refuse_unclassified`, the closure oracle
:func:`repoach.forge.closure.judge`) against the known values of its own
structured fields, the public forges' hosts and the deny-list before any
file is written; a refusal is exit 2 and nothing is written. A
second run changes nothing, which
``tests/unit/test_forge_recording_redaction.py`` pins.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from repoach.core.denylist import DenylistError
from repoach.core.logging import route_default_logs_to_stderr
from repoach.forge.fixture_pipeline import (
    ResidualValueError,
    UnknownForgeError,
    redact_corpus,
)
from repoach.forge.recording import NeedleError, SyntheticIdsExhaustedError

__all__ = ["FIXTURE_ROOT", "main"]

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "forge"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: one line on stdout, or nothing and exit 2 on a refusal.

    Args:
        argv: The command line after the program name; ``sys.argv[1:]`` when ``None``.

    Returns:
        ``0`` when done, ``1`` under ``--check`` when a file would change, ``2`` on a refusal.
    """
    route_default_logs_to_stderr()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=Path, default=FIXTURE_ROOT)
    parser.add_argument("--check", action="store_true")
    ns = parser.parse_args(argv)
    try:
        changed = redact_corpus(ns.root, check=ns.check)
    except (
        UnknownForgeError,
        SyntheticIdsExhaustedError,
        NeedleError,
        ResidualValueError,
        DenylistError,
    ) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    verb = "would change" if ns.check else "changed"
    sys.stdout.write(f"forge fixtures: {len(changed)} {verb}\n")
    return 1 if ns.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
