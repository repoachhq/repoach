"""Corpus validation and label stubs of ``repoach bench`` (SP-PRECISION-MEASUREMENT G3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from repoach.bench.corpus import (
    CorpusError,
    add_stubs,
    fingerprint,
    load_case,
    load_corpus,
    write_case,
)


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _local_case(tmp_path: Path, **extra: object) -> dict:
    (tmp_path / "fixture").mkdir(exist_ok=True)
    (tmp_path / "p.patch").write_text("", encoding="utf-8")
    return {
        "case": "c1",
        "source": {"kind": "local", "fixture": "fixture", "patch": "p.patch"},
        **extra,
    }


def test_yaml_booleans_are_labels(tmp_path: Path) -> None:
    data = _local_case(
        tmp_path,
        findings=[
            {"fingerprint": "a" * 12, "label": True, "labeller": "owner"},
            {"fingerprint": "b" * 12, "label": False, "labeller": "owner"},
            {"fingerprint": "c" * 12, "label": "unsure", "labeller": "owner"},
            {"fingerprint": "d" * 12, "label": None},
        ],
    )
    case = load_case(_write(tmp_path / "c1.yaml", data))
    assert case.labels() == {
        "a" * 12: "true",
        "b" * 12: "false",
        "c" * 12: "unsure",
        "d" * 12: None,
    }


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"findings": [{"fingerprint": "a" * 12, "label": True}]}, "needs a labeller"),
        ({"findings": [{"fingerprint": "xyz", "label": None}]}, "findings.0.fingerprint"),
        (
            {
                "findings": [
                    {"fingerprint": "a" * 12, "label": None},
                    {"fingerprint": "a" * 12, "label": None},
                ]
            },
            "labelled twice",
        ),
        (
            {"planted": [{"file": "a.py", "lines": [5, 2], "kind": "logic", "severity": "major"}]},
            "first <= last",
        ),
        (
            {"planted": [{"file": "a.py", "lines": [1, 2], "kind": "style", "severity": "major"}]},
            "planted.0.kind",
        ),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_invalid_case_names_file_and_field(tmp_path: Path, extra: dict, message: str) -> None:
    path = _write(tmp_path / "c1.yaml", _local_case(tmp_path, **extra))
    with pytest.raises(CorpusError, match=message) as info:
        load_case(path)
    assert str(path) in str(info.value)


def test_local_source_paths_must_exist(tmp_path: Path) -> None:
    data = {"case": "c1", "source": {"kind": "local", "fixture": "nope", "patch": "nope.patch"}}
    with pytest.raises(CorpusError, match="does not exist"):
        load_case(_write(tmp_path / "c1.yaml", data))


def test_forge_and_ledger_sources_need_their_fields(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="needs repo and pr"):
        load_case(_write(tmp_path / "a.yaml", {"case": "a", "source": {"kind": "forge"}}))
    ledger = {"kind": "ledger", "repo": "https://gitlab.com/o/r", "pr": 3}
    with pytest.raises(CorpusError, match="needs head_sha"):
        load_case(_write(tmp_path / "b.yaml", {"case": "b", "source": ledger}))


def test_toml_case_reads_like_yaml(tmp_path: Path) -> None:
    (tmp_path / "c.toml").write_text(
        'case = "t1"\n[source]\nkind = "forge"\nrepo = "https://github.com/o/r"\npr = 7\n',
        encoding="utf-8",
    )
    corpus = load_corpus(tmp_path / "c.toml")
    assert corpus.name == "c"
    assert corpus.cases[0].source.pr == 7


def test_corpus_refuses_duplicate_ids_and_empty_dirs(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="no case file"):
        load_corpus(tmp_path)
    source = {"kind": "forge", "repo": "https://github.com/o/r", "pr": 1}
    _write(tmp_path / "a.yaml", {"case": "same", "source": source})
    _write(tmp_path / "b.yaml", {"case": "same", "source": source})
    with pytest.raises(CorpusError, match="used twice: same"):
        load_corpus(tmp_path)
    with pytest.raises(CorpusError, match="no such corpus"):
        load_corpus(tmp_path / "missing")


def test_stubs_round_trip_as_a_one_line_edit(tmp_path: Path) -> None:
    path = _write(tmp_path / "c1.yaml", _local_case(tmp_path))
    case = load_case(path)
    assert add_stubs(case, [("a" * 12, "sentinel blocker a.py:3: injection")]) == 1
    assert add_stubs(case, [("a" * 12, "again")]) == 0
    write_case(case)
    text = path.read_text(encoding="utf-8")
    assert "label: null" in text
    assert "labeller: null" in text
    assert "labelled_on: null" in text
    assert "sentinel blocker a.py:3: injection" in text
    edited = (
        text.replace("label: null", "label: true")
        .replace("labeller: null", "labeller: owner")
        .replace("labelled_on: null", "labelled_on: 2026-10-21")
    )
    path.write_text(edited, encoding="utf-8")
    assert load_case(path).labels() == {"a" * 12: "true"}
    write_case(load_case(path))
    assert "label: true" in path.read_text(encoding="utf-8")


def test_private_case_stubs_carry_no_hint(tmp_path: Path) -> None:
    case = load_case(_write(tmp_path / "c1.yaml", _local_case(tmp_path, private=True)))
    add_stubs(case, [("a" * 12, "secret claim text")])
    write_case(case)
    assert "secret" not in (tmp_path / "c1.yaml").read_text(encoding="utf-8")


def test_fingerprint_ignores_wording_and_nearby_lines() -> None:
    first = fingerprint(reviewer="Sentinel", claim_type="security", file="a.py", line_start=11)
    again = fingerprint(reviewer="sentinel", claim_type="security", file="a.py", line_start=13)
    moved = fingerprint(reviewer="sentinel", claim_type="security", file="a.py", line_start=16)
    assert first == again
    assert first != moved
    assert len(first) == 12
