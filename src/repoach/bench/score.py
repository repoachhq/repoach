"""Precision, refuter precision and recall of a recorded run against the corpus labels.

Definitions (SP-PRECISION-MEASUREMENT G1, G2), over one finding's
human label and its final status:

========================  ==============  ==============
label                     not refuted     refuted
========================  ==============  ==============
``true``                  true positive   wrong refutation
``false``                 false positive  correct refutation
``unsure``                unsure (excluded from every ratio)
none yet                  unlabelled (excluded, listed for labelling)
========================  ==============  ==============

precision = TP / (TP + FP); refuter precision = correct refutations /
(correct + wrong refutations); recall = planted defects found / planted
defects, where a defect is *found* when a non-refuted finding labelled
``true`` cites its file on a line range overlapping the defect's. A
defect whose overlapping findings are not all labelled yet (and none is
labelled ``true``) is left out of recall and counted as waiting on
labels, so an unlabelled run never reads as a recall of 0. Every
ratio is printed with its numerator and denominator, and ``cannot say``
when the denominator is 0.

Rows: per reviewer, a defect counts as found by the reviewers whose
findings found it; per severity, TP / FP are keyed by the finding's
severity and recall by the planted defect's severity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .corpus import Corpus, Label, PlantedDefect
from .result import BenchResult, RecordedFinding

REVIEWERS: tuple[str, ...] = ("architect", "sentinel", "tester", "scribe")
SEVERITIES: tuple[str, ...] = ("blocker", "major", "minor", "nit")
TABLE_COLUMNS: tuple[str, ...] = (
    "TP",
    "FP",
    "unsure",
    "precision",
    "refuter precision",
    "recall",
)
CORPUS_MIN_LABELLED = 100
"""``constraints.corpus_min_labelled_findings`` of SP-PRECISION-MEASUREMENT."""

LABELS_STALE_BELOW = 0.70
"""A1: under this share of a run's labelled fingerprints found again, the labels are stale."""

CANNOT_SAY = "cannot say"


class ReadmeLineRefusedError(ValueError):
    """The run cannot carry the published line; the message says why."""


@dataclass(frozen=True)
class Ratio:
    """A ratio printed with its parts."""

    num: int
    den: int

    def text(self) -> str:
        """Return ``0.75 (3/4)``, or ``cannot say`` when the denominator is 0."""
        if self.den == 0:
            return CANNOT_SAY
        return f"{self.num / self.den:.2f} ({self.num}/{self.den})"

    def fraction(self) -> str:
        """Return ``3/4``, or ``cannot say`` when the denominator is 0."""
        return CANNOT_SAY if self.den == 0 else f"{self.num}/{self.den}"


@dataclass
class Tally:
    """The counts behind one table row."""

    tp: int = 0
    fp: int = 0
    unsure: int = 0
    unlabelled: int = 0
    correct_refutations: int = 0
    wrong_refutations: int = 0
    planted: int = 0
    found: int = 0

    def add(self, label: Label | None, refuted: bool) -> None:
        """Count one finding by its label and its final status."""
        if label is None:
            self.unlabelled += 1
        elif label == "unsure":
            self.unsure += 1
        elif label == "true":
            if refuted:
                self.wrong_refutations += 1
            else:
                self.tp += 1
        elif refuted:
            self.correct_refutations += 1
        else:
            self.fp += 1

    @property
    def labelled(self) -> int:
        """Return the findings carrying a human label (``unsure`` included)."""
        return self.tp + self.fp + self.unsure + self.correct_refutations + self.wrong_refutations

    @property
    def precision(self) -> Ratio:
        """Return TP / (TP + FP)."""
        return Ratio(self.tp, self.tp + self.fp)

    @property
    def refuter_precision(self) -> Ratio:
        """Return correct refutations / labelled refutations."""
        return Ratio(self.correct_refutations, self.correct_refutations + self.wrong_refutations)

    @property
    def recall(self) -> Ratio:
        """Return planted defects found / planted defects."""
        return Ratio(self.found, self.planted)

    def cells(self) -> list[str]:
        """Return the cells under :data:`TABLE_COLUMNS`."""
        return [
            str(self.tp),
            str(self.fp),
            str(self.unsure),
            self.precision.text(),
            self.refuter_precision.text(),
            self.recall.text(),
        ]


@dataclass(frozen=True)
class CaseLine:
    """Per-case facts printed under the tables."""

    case: str
    findings: int
    refuted: int
    planted: int
    located: int
    tokens: int
    wall_s: float
    skipped: str | None


@dataclass(frozen=True)
class Unlabelled:
    """A finding waiting for a label."""

    case: str
    finding: RecordedFinding


@dataclass
class Scorecard:
    """Everything ``bench report`` prints."""

    corpus: str
    corpus_sha: str
    provider: str
    measured_on: str
    partial: bool
    dry_run: bool
    total: Tally
    by_reviewer: dict[str, Tally]
    by_severity: dict[str, Tally]
    cases: list[CaseLine]
    cases_skipped: int
    planted_located: int
    planted_total: int
    recall_pending: int
    labels_expected: int
    labels_matched: int
    unlabelled: list[Unlabelled] = field(default_factory=list)

    @property
    def cases_scored(self) -> int:
        """Return the cases that were reviewed (not skipped)."""
        return len(self.cases) - self.cases_skipped

    @property
    def match_rate(self) -> Ratio:
        """Return the labelled fingerprints found again / labelled fingerprints (A1)."""
        return Ratio(self.labels_matched, self.labels_expected)

    @property
    def labels_stale(self) -> bool:
        """Return True when fewer than 70 % of the labelled fingerprints were found again."""
        rate = self.match_rate
        return rate.den > 0 and rate.num / rate.den < LABELS_STALE_BELOW


def overlaps(finding: RecordedFinding, defect: PlantedDefect) -> bool:
    """Return True when the finding cites the defect's file on an overlapping line range."""
    first, last = defect.lines
    return (
        finding.file == defect.file
        and finding.line_start <= last
        and max(finding.line_end, finding.line_start) >= first
    )


def _row(table: dict[str, Tally], key: str) -> Tally:
    return table.setdefault(key, Tally())


def _ordered(table: dict[str, Tally], keys: Iterable[str]) -> dict[str, Tally]:
    known = list(keys)
    ordered = {key: table.get(key, Tally()) for key in known}
    for key in sorted(set(table) - set(known)):
        ordered[key] = table[key]
    return ordered


def score(result: BenchResult, corpus: Corpus) -> Scorecard:
    """Score a recorded run against the labels and planted defects of its corpus.

    Args:
        result: The recorded run.
        corpus: The corpus, read now (labels added after the run count).

    Returns:
        The scorecard.
    """
    total = Tally()
    by_reviewer: dict[str, Tally] = {}
    by_severity: dict[str, Tally] = {}
    lines: list[CaseLine] = []
    unlabelled: list[Unlabelled] = []
    skipped = located_total = expected = matched = planted_total = recall_pending = 0
    known_cases = {case.case: case for case in corpus.cases}
    for run in result.cases:
        case = known_cases.get(run.case)
        labels = case.labels() if case is not None else {}
        planted = case.planted if case is not None else []
        if run.skipped is not None:
            skipped += 1
            lines.append(CaseLine(run.case, 0, 0, len(planted), 0, 0, 0.0, run.skipped))
            continue
        seen = {finding.fingerprint for finding in run.findings}
        labelled_prints = {fp for fp, label in labels.items() if label is not None}
        expected += len(labelled_prints)
        matched += len(labelled_prints & seen)
        for finding in run.findings:
            label = labels.get(finding.fingerprint)
            total.add(label, finding.refuted)
            _row(by_reviewer, finding.reviewer).add(label, finding.refuted)
            _row(by_severity, finding.severity).add(label, finding.refuted)
            if label is None:
                unlabelled.append(Unlabelled(run.case, finding))
        located = 0
        for defect in planted:
            planted_total += 1
            posted = [f for f in run.findings if not f.refuted and overlaps(f, defect)]
            if posted:
                located += 1
            confirmed = [f for f in posted if labels.get(f.fingerprint) == "true"]
            if not confirmed and any(labels.get(f.fingerprint) is None for f in posted):
                recall_pending += 1
                continue
            total.planted += 1
            _row(by_severity, defect.severity).planted += 1
            for reviewer in REVIEWERS:
                _row(by_reviewer, reviewer).planted += 1
            if confirmed:
                total.found += 1
                _row(by_severity, defect.severity).found += 1
                for reviewer in sorted({f.reviewer for f in confirmed}):
                    _row(by_reviewer, reviewer).found += 1
        located_total += located
        lines.append(
            CaseLine(
                case=run.case,
                findings=len(run.findings),
                refuted=sum(1 for f in run.findings if f.refuted),
                planted=len(planted),
                located=located,
                tokens=run.tokens,
                wall_s=run.wall_s,
                skipped=None,
            )
        )
    return Scorecard(
        corpus=result.corpus,
        corpus_sha=result.corpus_sha,
        provider=result.provider,
        measured_on=result.measured_on.isoformat(),
        partial=result.partial,
        dry_run=result.dry_run,
        total=total,
        by_reviewer=_ordered(by_reviewer, REVIEWERS),
        by_severity=_ordered(by_severity, SEVERITIES),
        cases=lines,
        cases_skipped=skipped,
        planted_located=located_total,
        planted_total=planted_total,
        recall_pending=recall_pending,
        labels_expected=expected,
        labels_matched=matched,
        unlabelled=unlabelled,
    )


def _table(key: str, rows: dict[str, Tally], *, markdown: bool) -> list[str]:
    header = [key, *TABLE_COLUMNS]
    body = [[name, *tally.cells()] for name, tally in rows.items()]
    if not markdown:
        return [" | ".join(header), *(" | ".join(cells) for cells in body)]
    rule = ["---"] + ["---:"] * len(TABLE_COLUMNS)
    return [f"| {' | '.join(cells)} |" for cells in (header, rule, *body)]


def headline(card: Scorecard) -> str:
    """Return the run's header line (``partial`` named when the run stopped early)."""
    flags = []
    if card.partial:
        flags.append("partial")
    if card.dry_run:
        flags.append("dry run")
    suffix = f" [{', '.join(flags)}]" if flags else ""
    return (
        f"corpus {card.corpus}@{card.corpus_sha}, provider {card.provider}, "
        f"measured {card.measured_on}, {card.cases_scored} cases scored, "
        f"{card.cases_skipped} cases skipped, {card.total.labelled} labelled findings"
        f"{suffix}"
    )


def totals_line(card: Scorecard) -> str:
    """Return the totals: ``precision 3/4 = 0.75, refuter precision 1/2 = 0.50, …``."""

    def part(name: str, ratio: Ratio) -> str:
        if ratio.den == 0:
            return f"{name} {CANNOT_SAY}"
        return f"{name} {ratio.fraction()} = {ratio.num / ratio.den:.2f}"

    t = card.total
    return ", ".join(
        [
            part("precision", t.precision),
            part("refuter precision", t.refuter_precision),
            part("recall", t.recall),
            f"unsure {t.unsure}",
            f"unlabelled {t.unlabelled}",
        ]
    )


def render_report(card: Scorecard, *, markdown: bool = False) -> list[str]:
    """Return the report: header, the two tables, the totals and the per-case facts."""
    lines = [headline(card), ""]
    lines += _table("reviewer", card.by_reviewer, markdown=markdown)
    lines.append("")
    lines += _table("severity", card.by_severity, markdown=markdown)
    lines += ["", totals_line(card)]
    lines.append(
        f"planted defects located: {card.planted_located}/{card.planted_total} (a non-refuted"
        f" finding overlaps the defect); recall waits on labels for {card.recall_pending}"
    )
    if card.labels_expected:
        lines.append(f"label match rate: {card.match_rate.text()}")
    lines.append("")
    case_header = ["case", "findings", "refuted", "planted", "located", "tokens", "wall s"]
    rows = [
        [
            c.case,
            str(c.findings),
            str(c.refuted),
            str(c.planted),
            str(c.located),
            str(c.tokens),
            f"{c.wall_s:.0f}",
        ]
        if c.skipped is None
        else [c.case, f"skipped: {c.skipped}", "", str(c.planted), "", "", ""]
        for c in card.cases
    ]
    if markdown:
        rule = ["---"] + ["---:"] * (len(case_header) - 1)
        lines += [f"| {' | '.join(r)} |" for r in (case_header, rule, *rows)]
    else:
        lines += [" | ".join(r) for r in (case_header, *rows)]
    return lines


def readme_line(card: Scorecard, *, minimum: int = CORPUS_MIN_LABELLED) -> str:
    """Return the published line (G5), or refuse.

    Raises:
        ReadmeLineRefusedError: The run is partial or a dry run, the corpus
            has fewer than ``minimum`` labelled findings, or precision or
            recall has a zero denominator.
    """
    if card.partial:
        raise ReadmeLineRefusedError("partial run: no published line from a partial run")
    if card.dry_run:
        raise ReadmeLineRefusedError("dry run: no finding was produced")
    labelled = card.total.labelled
    if labelled < minimum:
        raise ReadmeLineRefusedError(f"corpus too small: {labelled} < {minimum}")
    t = card.total
    if t.precision.den == 0:
        raise ReadmeLineRefusedError(
            "precision cannot say: no labelled true or false posted finding"
        )
    if t.recall.den == 0:
        raise ReadmeLineRefusedError("recall cannot say: no planted defect in the corpus")
    return (
        f"precision {t.precision.text()}, recall {t.recall.text()} on {labelled} labelled "
        f"findings across {card.cases_scored} cases, corpus {card.corpus}@{card.corpus_sha}, "
        f"measured {card.measured_on}, provider {card.provider}"
    )


__all__ = [
    "CANNOT_SAY",
    "CORPUS_MIN_LABELLED",
    "LABELS_STALE_BELOW",
    "REVIEWERS",
    "SEVERITIES",
    "TABLE_COLUMNS",
    "CaseLine",
    "Ratio",
    "ReadmeLineRefusedError",
    "Scorecard",
    "Tally",
    "Unlabelled",
    "headline",
    "overlaps",
    "readme_line",
    "render_report",
    "score",
    "totals_line",
]
