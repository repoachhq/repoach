"""Integration test for diff_scoper — end-to-end scope_diff with a three-unit diff."""

from __future__ import annotations

from ferova.review.diff_scoper import scope_diff, split_diff


def test_scope_diff_three_unit_end_to_end() -> None:
    """split_diff yields three units; scope_diff with cap=units[0].chars+10 includes only one."""
    synthetic_diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "index 000..001 100644\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1 +1 @@\n"
        "-old_a\n"
        "+new_a\n"
        "diff --git a/src/b.py b/src/b.py\n"
        "index 000..001 100644\n"
        "--- a/src/b.py\n"
        "+++ b/src/b.py\n"
        "@@ -1 +1 @@\n"
        "-old_b\n"
        "+new_b\n"
        "diff --git a/src/c.py b/src/c.py\n"
        "index 000..001 100644\n"
        "--- a/src/c.py\n"
        "+++ b/src/c.py\n"
        "@@ -1 +1 @@\n"
        "-old_c\n"
        "+new_c\n"
    )
    units = split_diff(synthetic_diff)
    assert len(units) == 3
    cap = units[0].chars + 10
    scoped = scope_diff(synthetic_diff, cap)
    assert len(scoped.included) == 1
    assert len(scoped.omitted) == 2
