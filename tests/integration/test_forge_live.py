"""SP-UNIVERSAL-FORGE NG4 — read-only probes of both forges against live change requests.

Opt-in: runs only with ``REPOACH_FORGE_LIVE=1`` (the unit conformance
suite replays recorded fixtures and never touches the network). Each
probe is a read; the GitLab probe asserts the MR's note count is the
same before and after, so a regression that writes is caught.

Targets (override with the variables below):

* ``REPOACH_FORGE_LIVE_GITHUB_CWD`` — a clone of repoachhq/repoach
  (default: this checkout), PR ``REPOACH_FORGE_LIVE_GITHUB_PR`` (200).
* ``REPOACH_FORGE_LIVE_GITLAB_CWD`` — a clone of a GitLab project,
  MR ``REPOACH_FORGE_LIVE_GITLAB_MR`` (283).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repoach.forge import ARCHIVE_MARKER, select_forge

LIVE = os.environ.get("REPOACH_FORGE_LIVE") == "1"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(not LIVE, reason="live forge probes need REPOACH_FORGE_LIVE=1")


def _probe(forge_name: str, cwd: Path, number: int) -> None:
    """Exercise every read the review / gate path makes on one change."""
    forge = select_forge(override=forge_name, cwd=cwd)
    before = len(forge.conversation_comments(number))
    pr = forge.pull_request(number)
    assert pr.number == number and pr.head_ref and pr.base_ref
    assert forge.pr_state(number) == pr.state
    assert forge.head_sha(number) == pr.head_sha
    diff = forge.diff(number)
    assert diff.files and diff.text.count("diff --git a/") == len(diff.files)
    forge.comments(number)
    forge.reviews(number)
    forge.find_marked_comment(number, ARCHIVE_MARKER)
    assert forge.ci_status(number, head_sha=pr.head_sha).state in {
        "success",
        "failure",
        "pending",
        "missing",
    }
    forge.branch_protection(pr.base_ref)
    assert len(forge.conversation_comments(number)) == before


def test_github_live_reads() -> None:
    cwd = Path(os.environ.get("REPOACH_FORGE_LIVE_GITHUB_CWD", str(REPO_ROOT)))
    _probe("github", cwd, int(os.environ.get("REPOACH_FORGE_LIVE_GITHUB_PR", "200")))


def test_gitlab_live_reads() -> None:
    raw_cwd = os.environ.get("REPOACH_FORGE_LIVE_GITLAB_CWD")
    if not raw_cwd:
        pytest.fail("set REPOACH_FORGE_LIVE_GITLAB_CWD to a GitLab clone")
    _probe("gitlab", Path(raw_cwd), int(os.environ.get("REPOACH_FORGE_LIVE_GITLAB_MR", "283")))
