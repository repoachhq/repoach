"""Unit tests for SP-TRUNCATE-CAP-NAMED-CONST.

Pins the two named truncation-cap constants that replace the
previously-bare ``32000`` literals: ``persistence._PERSISTED_JSON_TRUNCATE_CHARS``
(backing ``record_dialogue``'s ``payload_json`` write and
``record_coder_response``'s ``fixes_json`` write) and
``reviewer._EXISTING_FILE_PROMPT_TRUNCATE_CHARS`` (backing
``_format_existing_files``'s prompt-block truncation), and asserts the
two stay distinct symbols with no new cross-module import between
``reviewer.py`` and ``persistence.py``.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from sqlalchemy import create_engine, text

from repoach.review import reviewer
from repoach.review.persistence import (
    _PERSISTED_JSON_TRUNCATE_CHARS,
    init_schema,
    record_coder_response,
    record_dialogue,
)
from repoach.review.reviewer import (
    _EXISTING_FILE_PROMPT_TRUNCATE_CHARS,
    _format_existing_files,
)


def _payload_of_serialized_length(target: int) -> dict[str, str]:
    base_len = len(json.dumps({"v": ""}, default=str))
    return {"v": "x" * (target - base_len)}


def _fixes_of_serialized_length(target: int) -> list[str]:
    base_len = len(json.dumps([""]))
    return ["x" * (target - base_len)]


def _raw_column(db_path: Path, table: str, column: str) -> str:
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT {column} FROM {table} ORDER BY id DESC LIMIT 1")).one()
    return str(row[0])


def test_persisted_json_truncate_chars_constant_backs_dialogue_write(
    tmp_path: Path,
) -> None:
    assert _PERSISTED_JSON_TRUNCATE_CHARS == 32000
    db_path = tmp_path / "l4.sqlite"
    init_schema(db_path)

    record_dialogue(
        db_path,
        pr_number=1,
        round="1",
        speaker="architect",
        payload=_payload_of_serialized_length(_PERSISTED_JSON_TRUNCATE_CHARS),
    )
    assert len(_raw_column(db_path, "pr_review_dialogue", "payload_json")) == (
        _PERSISTED_JSON_TRUNCATE_CHARS
    )

    record_dialogue(
        db_path,
        pr_number=1,
        round="1",
        speaker="sentinel",
        payload=_payload_of_serialized_length(_PERSISTED_JSON_TRUNCATE_CHARS + 1),
    )
    assert len(_raw_column(db_path, "pr_review_dialogue", "payload_json")) == (
        _PERSISTED_JSON_TRUNCATE_CHARS
    )


def test_persisted_json_truncate_chars_constant_backs_coder_response_write(
    tmp_path: Path,
) -> None:
    assert _PERSISTED_JSON_TRUNCATE_CHARS == 32000
    db_path = tmp_path / "l4.sqlite"
    init_schema(db_path)

    record_coder_response(
        db_path,
        pr_number=1,
        plan={"fixes": _fixes_of_serialized_length(_PERSISTED_JSON_TRUNCATE_CHARS)},
        model_used="kimi-k2-instruct",
        elapsed_s=1.0,
        tokens_used=10,
    )
    assert len(_raw_column(db_path, "pr_coder_responses", "fixes_json")) == (
        _PERSISTED_JSON_TRUNCATE_CHARS
    )

    record_coder_response(
        db_path,
        pr_number=1,
        plan={"fixes": _fixes_of_serialized_length(_PERSISTED_JSON_TRUNCATE_CHARS + 1)},
        model_used="kimi-k2-instruct",
        elapsed_s=1.0,
        tokens_used=10,
    )
    assert len(_raw_column(db_path, "pr_coder_responses", "fixes_json")) == (
        _PERSISTED_JSON_TRUNCATE_CHARS
    )


def test_existing_file_prompt_truncate_chars_is_a_distinct_constant() -> None:
    assert _EXISTING_FILE_PROMPT_TRUNCATE_CHARS == 32000
    assert _EXISTING_FILE_PROMPT_TRUNCATE_CHARS is not _PERSISTED_JSON_TRUNCATE_CHARS
    assert "persistence" not in inspect.getsource(reviewer)


def test_format_existing_files_truncates_at_named_constant_length() -> None:
    exact = "a" * _EXISTING_FILE_PROMPT_TRUNCATE_CHARS
    over = "a" * (_EXISTING_FILE_PROMPT_TRUNCATE_CHARS + 1)

    rendered_exact = _format_existing_files({"exact.py": exact})
    assert "[... file truncated" not in rendered_exact
    assert exact in rendered_exact

    rendered_over = _format_existing_files({"over.py": over})
    assert "[... file truncated" in rendered_over
    body = rendered_over.split("=== over.py ===\n", 1)[1]
    truncated_content = body.split("\n# [... file truncated", 1)[0]
    assert len(truncated_content) == _EXISTING_FILE_PROMPT_TRUNCATE_CHARS
