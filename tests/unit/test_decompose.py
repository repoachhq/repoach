"""Tests for SP-DEVAGENT-DECOMPOSE (slice 4): the sub-spec decomposition front-end.

Focus: the identity passthrough (no proposer call), partition validation (cover +
disjoint + in-bounds + declared edges + acyclic), retry-then-accept, persistent
failure, dependency ordering, and the markdown round-trip through the arch loader.
"""

from __future__ import annotations

import json
from pathlib import Path

from repoach.review.decompose import (
    SubSpec,
    decompose_spec,
    render_sub_spec_markdown,
)
from repoach.review.governed_spec import GovernedSpec, load_governed_spec
from repoach.review.spec import SpecPlan


def _spec() -> SpecPlan:
    return SpecPlan(
        id="SP-PARENT",
        file_path=Path("docs/specs/2026-06-28_SP-PARENT_demo.md"),
        raw_markdown="# SP-PARENT\n\nbody\n",
        title="Parent",
        summary="parent summary",
    )


def _governed(
    owns_code: list[str],
    depends_on: tuple[str, ...] = (),
    owns_resources: tuple[str, ...] = (),
) -> GovernedSpec:
    return GovernedSpec(
        id="SP-PARENT",
        owns_code=tuple(owns_code),
        owns_resources=owns_resources,
        depends_on=depends_on,
    )


class _Proposer:
    """Callable proposer stand-in replaying canned replies and counting calls."""

    def __init__(self, *replies: str) -> None:
        self._replies = replies
        self._i = 0
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        reply = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        return reply


def _proposal(*subs: dict) -> str:
    return json.dumps({"sub_specs": list(subs)})


def _sub(
    sub_id: str,
    owns_code: list[str],
    depends_on: list[str],
    owns_resources: list[str] | None = None,
) -> dict:
    return {
        "id": sub_id,
        "title": sub_id,
        "summary": "s",
        "owns_code": owns_code,
        "owns_resources": owns_resources or [],
        "depends_on": depends_on,
        "body": "## Goals\n- g\n\n## Acceptance Criteria\n- [ ] ac\n",
    }


def test_identity_passthrough_does_not_call_proposer() -> None:
    proposer = _Proposer("should not be called")
    result = decompose_spec(
        _spec(), _governed(["src/repoach/x.py"]), proposer=proposer, repo_root=Path(".")
    )
    assert result.identity is True
    assert result.error is None
    assert len(result.sub_specs) == 1
    assert result.sub_specs[0].id == "SP-PARENT"
    assert proposer.calls == 0


def test_valid_two_way_partition_accepted_and_ordered() -> None:
    governed = _governed(["src/a.py", "src/b.py"], depends_on=("SP-DEP",))
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-2", ["src/b.py"], ["SP-PARENT-1"]),
            _sub("SP-PARENT-1", ["src/a.py"], ["SP-DEP"]),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is None
    assert result.identity is False
    assert [s.id for s in result.sub_specs] == ["SP-PARENT-1", "SP-PARENT-2"]


def test_uncovered_parent_path_is_rejected() -> None:
    governed = _governed(["src/a.py", "src/b.py"])
    proposer = _Proposer(_proposal(_sub("SP-PARENT-1", ["src/a.py"], [])))
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert "cover" in result.error or "uncovered" in result.error


def test_duplicate_owned_path_is_rejected() -> None:
    governed = _governed(["src/a.py", "src/b.py"])
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-1", ["src/a.py"], []),
            _sub("SP-PARENT-2", ["src/a.py", "src/b.py"], []),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert "more than one" in result.error


def test_out_of_parent_path_is_rejected() -> None:
    governed = _governed(["src/a.py", "src/b.py"])
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-1", ["src/a.py"], []),
            _sub("SP-PARENT-2", ["src/c.py"], []),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert "parent does not" in result.error


def test_undeclared_edge_is_rejected() -> None:
    governed = _governed(["src/a.py", "src/b.py"])
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-1", ["src/a.py"], ["SP-OTHER"]),
            _sub("SP-PARENT-2", ["src/b.py"], []),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert "depends on" in result.error


def test_cycle_is_rejected() -> None:
    governed = _governed(["src/a.py", "src/b.py"])
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-1", ["src/a.py"], ["SP-PARENT-2"]),
            _sub("SP-PARENT-2", ["src/b.py"], ["SP-PARENT-1"]),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert "cycle" in result.error


def test_retry_then_accept() -> None:
    governed = _governed(["src/a.py", "src/b.py"])
    bad = _proposal(_sub("SP-PARENT-1", ["src/a.py"], []))
    good = _proposal(
        _sub("SP-PARENT-1", ["src/a.py"], []),
        _sub("SP-PARENT-2", ["src/b.py"], []),
    )
    proposer = _Proposer(bad, good)
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is None
    assert proposer.calls == 2
    assert [s.id for s in result.sub_specs] == ["SP-PARENT-1", "SP-PARENT-2"]


def test_persistent_failure_returns_error() -> None:
    governed = _governed(["src/a.py", "src/b.py"])
    proposer = _Proposer("not json at all")
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert result.sub_specs == []
    assert proposer.calls == 3


def test_proposer_raises_returns_error() -> None:
    governed = _governed(["src/a.py", "src/b.py"])

    def _boom(prompt: str) -> str:
        raise RuntimeError("proxy down")

    result = decompose_spec(_spec(), governed, proposer=_boom, repo_root=Path("."))
    assert result.error is not None
    assert "proposer failed" in result.error


def test_traversal_sub_id_is_rejected() -> None:
    governed = _governed(["src/a.py", "src/b.py"])
    proposer = _Proposer(
        _proposal(
            _sub("../../../tmp/evil", ["src/a.py"], []),
            _sub("SP-PARENT-2", ["src/b.py"], []),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert "not valid SP-IDs" in result.error


def test_non_unique_sub_ids_rejected() -> None:
    governed = _governed(["src/a.py", "src/b.py"])
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-1", ["src/a.py"], []),
            _sub("SP-PARENT-1", ["src/b.py"], []),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert "not unique" in result.error


def test_nested_path_overlap_rejected() -> None:
    governed = _governed(["src/x/", "src/x/y.py"])
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-1", ["src/x/"], []),
            _sub("SP-PARENT-2", ["src/x/y.py"], []),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert "nested paths" in result.error


def test_resource_partition_accepted() -> None:
    governed = _governed(
        ["src/a.py", "src/b.py"], owns_resources=("db:table:orders", "queue:topic:emit")
    )
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-1", ["src/a.py"], [], owns_resources=["db:table:orders"]),
            _sub("SP-PARENT-2", ["src/b.py"], [], owns_resources=["queue:topic:emit"]),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is None
    assert [s.id for s in result.sub_specs] == ["SP-PARENT-1", "SP-PARENT-2"]


def test_duplicate_resource_rejected() -> None:
    governed = _governed(
        ["src/a.py", "src/b.py"], owns_resources=("db:table:orders", "queue:topic:emit")
    )
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-1", ["src/a.py"], [], owns_resources=["db:table:orders"]),
            _sub(
                "SP-PARENT-2",
                ["src/b.py"],
                [],
                owns_resources=["db:table:orders", "queue:topic:emit"],
            ),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert "resource is claimed by more than one" in result.error


def test_out_of_parent_resource_rejected() -> None:
    governed = _governed(["src/a.py", "src/b.py"], owns_resources=("db:table:orders",))
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-1", ["src/a.py"], [], owns_resources=["db:table:orders"]),
            _sub("SP-PARENT-2", ["src/b.py"], [], owns_resources=["db:table:rogue"]),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is not None
    assert "parent does not" in result.error


def test_dependency_order_three_node_chain() -> None:
    governed = _governed(["src/a.py", "src/b.py", "src/c.py"])
    proposer = _Proposer(
        _proposal(
            _sub("SP-PARENT-3", ["src/c.py"], ["SP-PARENT-2"]),
            _sub("SP-PARENT-1", ["src/a.py"], []),
            _sub("SP-PARENT-2", ["src/b.py"], ["SP-PARENT-1"]),
        )
    )
    result = decompose_spec(_spec(), governed, proposer=proposer, repo_root=Path("."))
    assert result.error is None
    assert [s.id for s in result.sub_specs] == ["SP-PARENT-1", "SP-PARENT-2", "SP-PARENT-3"]


def test_render_sub_spec_round_trips_through_loader(tmp_path: Path) -> None:
    specs_dir = tmp_path / "docs" / "specs"
    specs_dir.mkdir(parents=True)
    sub = SubSpec(
        id="SP-SUB-1",
        title="Sub one: with a colon",
        summary="s",
        owns_code=("src/a.py",),
        owns_resources=("db:table:orders",),
        depends_on=("SP-DEP",),
        body="## Goals\n- g\n",
    )
    (specs_dir / "2026-06-28_SP-SUB-1_sub.md").write_text(
        render_sub_spec_markdown(sub), encoding="utf-8"
    )

    loaded = load_governed_spec("SP-SUB-1", root=tmp_path)
    assert loaded is not None
    assert loaded.id == "SP-SUB-1"
    assert loaded.owns_code == ("src/a.py",)
    assert loaded.owns_resources == ("db:table:orders",)
    assert loaded.depends_on == ("SP-DEP",)
