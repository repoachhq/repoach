"""The repository declares its affected-jobs corpus as a verified generated artefact."""

from __future__ import annotations

import os
from pathlib import Path

from repoach.project.config import parse_project_config
from repoach.review.generated_config import load_generated_entries, touched_artefacts

_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = "tests/fixtures/affected_jobs/pr_runs.json"


def _entries():
    text = (_ROOT / "repoach.toml").read_text(encoding="utf-8")
    languages = parse_project_config(text, _ROOT / "repoach.toml").project.languages
    return load_generated_entries(text, _ROOT / "repoach.toml", languages)


def test_corpus_is_declared_and_proved_by_replay() -> None:
    """A diff touching the corpus is summarised, and the check runs the replay."""
    touched = touched_artefacts(_entries(), [_CORPUS, "src/repoach/cli.py"])
    assert list(touched) == [_CORPUS]
    assert touched[_CORPUS].check == ("scripts/check_affected_corpus.sh",)


def test_check_script_is_tracked_executable_and_replays() -> None:
    """The check script is executable and does nothing but replay the corpus."""
    script = _ROOT / "scripts" / "check_affected_corpus.sh"
    assert os.access(script, os.X_OK)
    assert script.read_text(encoding="utf-8").splitlines()[-1] == "exec repoach ci affected replay"
