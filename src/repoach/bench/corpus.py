"""The labelled corpus: case files, their validation, and the label stubs.

A corpus is a directory of case files (``bench/corpus/<name>/*.yaml``)
or one case file; its name is the directory name or the file stem. A
case says where its diff comes from (``source``), which defects were
planted in it, and the human labels of the findings a run produced,
keyed by fingerprint. ``.toml`` case files are read the same way; the
label stubs are written to YAML files only.

The fingerprint deliberately leaves the claim text out (the spec's A1
names a claim hash): two runs of a model on the same head word the
same finding differently, so a hash of the words would carry no label
across runs. It hashes the reviewer, the claim type, the file and the
line window ``line_start // FINGERPRINT_WINDOW``.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

FINGERPRINT_WINDOW = 5
"""Lines per window of the fingerprint: a finding re-cited a few lines away still matches."""

CASE_SUFFIXES: tuple[str, ...] = (".yaml", ".yml", ".toml")

UNRECORDED_HEAD = "unrecorded"
"""A ledger case's ``head_sha`` for rows a dry run recorded with no head (before SP-REVIEW-PINS-HEAD)."""

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

Label = Literal["true", "false", "unsure"]
DefectKind = Literal["security", "logic", "test_gap", "docstring", "convention"]
PlantedSeverity = Literal["blocker", "major", "minor", "nit"]


class CorpusError(ValueError):
    """A corpus or case file is missing, unreadable or invalid; the message names the file."""


def fingerprint(*, reviewer: str, claim_type: str, file: str, line_start: int) -> str:
    """Return the 12-hex fingerprint labels are keyed by.

    Args:
        reviewer: The finder (``architect``, ``sentinel``, ``tester``, ``scribe``).
        claim_type: The finding's claim type.
        file: The cited repository-relative path.
        line_start: The first cited line.

    Returns:
        The first 12 hex digits of a SHA-256 over the four parts.
    """
    window = max(line_start, 0) // FINGERPRINT_WINDOW
    raw = f"{reviewer.lower()}|{claim_type}|{file}|{window}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseSource(_Strict):
    """Where a case's diff and findings come from.

    Attributes:
        kind: ``local`` (a fixture tree plus a patch, reviewed through a
            local forge), ``forge`` (a PR / MR of the repository the
            command runs in) or ``ledger`` (findings already recorded in
            a ledger, scored without a new review).
        fixture: ``local``: the base tree, relative to the case file.
        patch: ``local``: the unified diff applied on the base, relative
            to the case file.
        repo: ``forge`` / ``ledger``: the repository URL (for links).
        pr: ``forge`` / ``ledger``: the PR number or MR iid.
        head_sha: ``ledger``: the head whose findings are scored, or
            ``unrecorded`` for rows recorded with no head;
            ``forge``: the head expected (a mismatch skips the case).
    """

    kind: Literal["local", "forge", "ledger"]
    fixture: str | None = None
    patch: str | None = None
    repo: str | None = None
    pr: int | None = None
    head_sha: str | None = None

    @model_validator(mode="after")
    def _fields_for_kind(self) -> CaseSource:
        if self.kind == "local":
            if not self.fixture or not self.patch:
                raise ValueError("a local source needs fixture and patch")
        elif self.pr is None or not self.repo:
            raise ValueError(f"a {self.kind} source needs repo and pr")
        if self.kind == "ledger" and not (
            self.head_sha == UNRECORDED_HEAD or _FULL_SHA.fullmatch(self.head_sha or "")
        ):
            raise ValueError(f"a ledger source needs head_sha: 40 hex digits or {UNRECORDED_HEAD}")
        return self


class PlantedDefect(_Strict):
    """A defect deliberately introduced by a case's patch.

    Attributes:
        file: The file it lives in, relative to the reviewed tree.
        lines: The inclusive ``[first, last]`` range a finding must overlap.
        kind: The defect class.
        severity: The severity a careful reviewer would give it.
        note: What the defect is, for the human reading the corpus.
    """

    file: str
    lines: tuple[int, int]
    kind: DefectKind
    severity: PlantedSeverity
    note: str = ""

    @model_validator(mode="after")
    def _range_ordered(self) -> PlantedDefect:
        first, last = self.lines
        if first < 1 or last < first:
            raise ValueError(f"lines {list(self.lines)} must be 1 <= first <= last")
        return self


class LabelledFinding(_Strict):
    """The human label of one finding, keyed by its fingerprint.

    Attributes:
        fingerprint: :func:`fingerprint` of the finding.
        label: ``true``, ``false``, ``unsure``, or None while unlabelled.
        labeller: Who labelled it (a person; never a model, NG2).
        labelled_on: When.
        hint: Reviewer, location and claim excerpt to label from; empty
            for a private case.
    """

    fingerprint: str = Field(pattern=r"^[0-9a-f]{12}$")
    label: Label | None = None
    labeller: str | None = None
    labelled_on: date | None = None
    hint: str = ""

    @model_validator(mode="before")
    @classmethod
    def _yaml_booleans(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("label"), bool):
            return {**data, "label": "true" if data["label"] else "false"}
        return data

    @model_validator(mode="after")
    def _labeller_when_labelled(self) -> LabelledFinding:
        if self.label is not None and not self.labeller:
            raise ValueError(f"finding {self.fingerprint}: a label needs a labeller")
        return self


class Case(_Strict):
    """One corpus case.

    Attributes:
        case: The case id, unique in its corpus.
        description: What the case exercises.
        private: True when the reviewed code is not public: label stubs
            then carry no hint (NG3).
        source: Where the diff comes from.
        planted: The planted defects (empty for a real PR).
        findings: The labels.
        path: The case file (set by the loader, never written).
    """

    case: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    description: str = ""
    private: bool = False
    source: CaseSource
    planted: list[PlantedDefect] = Field(default_factory=list)
    findings: list[LabelledFinding] = Field(default_factory=list)
    path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _unique_fingerprints(self) -> Case:
        seen: set[str] = set()
        for entry in self.findings:
            if entry.fingerprint in seen:
                raise ValueError(f"fingerprint {entry.fingerprint} is labelled twice")
            seen.add(entry.fingerprint)
        return self

    def labels(self) -> dict[str, Label | None]:
        """Return ``{fingerprint: label}``, None for a stub not yet labelled."""
        return {entry.fingerprint: entry.label for entry in self.findings}

    def resolve(self, relative: str) -> Path:
        """Resolve a source path relative to the case file."""
        base = self.path.parent if self.path is not None else Path.cwd()
        return (base / relative).resolve()


class Corpus(BaseModel):
    """A named set of cases.

    Attributes:
        name: The directory name or the file stem.
        root: The corpus directory or case file.
        sha: First 12 hex digits of a SHA-256 over the case files, in
            name order: the corpus version the published number names.
        cases: The cases, in file-name order.
    """

    name: str
    root: Path
    sha: str
    cases: list[Case]

    def case(self, case_id: str) -> Case:
        """Return the case named ``case_id``.

        Raises:
            CorpusError: No such case.
        """
        for case in self.cases:
            if case.case == case_id:
                return case
        raise CorpusError(f"{self.root}: no case {case_id!r}")


def _read_mapping(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorpusError(f"{path}: unreadable: {exc.strerror}") from exc
    try:
        data = tomllib.loads(text) if path.suffix == ".toml" else yaml.safe_load(text)
    except (tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
        raise CorpusError(f"{path}: not valid {path.suffix[1:]}: {exc}") from exc
    if not isinstance(data, dict):
        raise CorpusError(f"{path}: a case file holds one mapping")
    return data


def load_case(path: Path) -> Case:
    """Read and validate one case file.

    Raises:
        CorpusError: The file is unreadable or invalid; the message names
            the file and the first invalid field.
    """
    data = _read_mapping(path)
    try:
        case = Case.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "case"
        raise CorpusError(f"{path}: {where}: {first['msg']}") from exc
    case.path = path.resolve()
    if case.source.kind == "local":
        for relative in (case.source.fixture, case.source.patch):
            if relative is not None and not case.resolve(relative).exists():
                raise CorpusError(f"{path}: source path {relative} does not exist")
    return case


def case_files(root: Path) -> list[Path]:
    """Return the case files of a corpus directory (or the file itself), in name order."""
    if root.is_file():
        return [root]
    return sorted(p for p in root.iterdir() if p.is_file() and p.suffix in CASE_SUFFIXES)


def load_corpus(root: Path) -> Corpus:
    """Read and validate every case of a corpus.

    Args:
        root: A corpus directory or a single case file.

    Returns:
        The corpus, its cases in file-name order.

    Raises:
        CorpusError: The path does not exist, holds no case, a case is
            invalid, or two cases share an id.
    """
    if not root.exists():
        raise CorpusError(f"{root}: no such corpus")
    files = case_files(root)
    if not files:
        raise CorpusError(f"{root}: no case file ({', '.join(CASE_SUFFIXES)})")
    cases = [load_case(path) for path in files]
    ids = [case.case for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise CorpusError(f"{root}: case ids used twice: {', '.join(duplicates)}")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    name = root.stem if root.is_file() else root.name
    return Corpus(name=name, root=root.resolve(), sha=digest.hexdigest()[:12], cases=cases)


_YAML_LABEL: dict[str | None, bool | str | None] = {"true": True, "false": False}
"""How a label is written back: YAML booleans for true / false, the word for unsure."""


def write_case(case: Case) -> Path:
    """Write a case back to its YAML file, preserving the field order.

    Raises:
        CorpusError: The case has no file, or its file is TOML.
    """
    if case.path is None:
        raise CorpusError(f"case {case.case}: no file to write")
    if case.path.suffix == ".toml":
        raise CorpusError(f"{case.path}: label stubs are written to YAML case files only")
    data = case.model_dump(mode="json", exclude_defaults=False)
    data["source"] = case.source.model_dump(mode="json", exclude_none=True)
    for entry in data["findings"]:
        entry["label"] = _YAML_LABEL.get(entry["label"], entry["label"])
        if not entry["hint"]:
            entry.pop("hint")
    for defect in data["planted"]:
        defect["lines"] = list(defect["lines"])
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    case.path.write_text(text, encoding="utf-8")
    return case.path


__all__ = [
    "CASE_SUFFIXES",
    "FINGERPRINT_WINDOW",
    "UNRECORDED_HEAD",
    "Case",
    "CaseSource",
    "Corpus",
    "CorpusError",
    "DefectKind",
    "Label",
    "LabelledFinding",
    "PlantedDefect",
    "PlantedSeverity",
    "add_stubs",
    "case_files",
    "fingerprint",
    "load_case",
    "load_corpus",
    "write_case",
]


def add_stubs(case: Case, entries: list[tuple[str, str]]) -> int:
    """Append an unlabelled stub per fingerprint the case does not list yet.

    Args:
        case: The case to extend (in memory; :func:`write_case` saves it).
        entries: ``(fingerprint, hint)`` pairs; the hint is dropped for a
            private case.

    Returns:
        The stubs added.
    """
    known = {entry.fingerprint for entry in case.findings}
    added = 0
    for print_, hint in entries:
        if print_ in known:
            continue
        known.add(print_)
        case.findings.append(LabelledFinding(fingerprint=print_, hint="" if case.private else hint))
        added += 1
    return added
