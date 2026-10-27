"""Record and replay the subprocess exchanges of a forge (conformance fixtures).

The conformance suite (``tests/unit/test_forge_conformance.py``) runs
every protocol method of both forges against fixtures under
``tests/fixtures/forge/<forge>/<case>.json``. A fixture is the ordered
list of subprocess exchanges one protocol call made — program
(``gh``/``glab``/``git``), argv after the program, stdin, exit code,
stdout, stderr — plus the normalised result (or the error class) the
call produced. ``scripts/record_forge_fixture.py`` writes them against a
real PR / MR; :class:`ReplaySpawn` plays them back and fails on the
first argv that differs from the recording, which is how the suite
pins the exact calls each forge makes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..core.logging import get_logger
from ..core.redaction import SECRET_VALUE_PATTERNS
from .protocol import InlineCommentDraft, MergeMethod, ReviewDraft
from .runner import CommandResult

_log = get_logger(__name__)

RECORDED_FIELDS: frozenset[str] = frozenset(
    {
        "a_mode",
        "allow_force_push",
        "allow_force_pushes",
        "approved_by",
        "author",
        "b_mode",
        "base_sha",
        "baseRefName",
        "body",
        "bucket",
        "checks",
        "conclusion",
        "context",
        "contexts",
        "created_at",
        "deleted_file",
        "description",
        "detailed_merge_status",
        "diff",
        "diff_refs",
        "draft",
        "enabled",
        "head_pipeline",
        "head_sha",
        "headRefName",
        "headRefOid",
        "id",
        "iid",
        "in_reply_to_id",
        "isDraft",
        "line",
        "link",
        "login",
        "merge_commit_sha",
        "merge_method",
        "mergeable",
        "mergeCommit",
        "mergeStateStatus",
        "message",
        "name",
        "new_file",
        "new_line",
        "new_path",
        "notes",
        "number",
        "oid",
        "old_line",
        "old_path",
        "only_allow_merge_if_pipeline_succeeds",
        "path",
        "position",
        "renamed_file",
        "required_status_checks",
        "sha",
        "source_branch",
        "squash_commit_sha",
        "stage",
        "start_sha",
        "started_at",
        "startedAt",
        "state",
        "status",
        "statusCheckRollup",
        "system",
        "target_branch",
        "title",
        "type",
        "url",
        "user",
        "username",
        "web_url",
        "work_in_progress",
        "workflow",
    }
)
"""Every JSON key the forges read from a ``gh`` / ``glab`` answer.

A recorded answer keeps only these keys (:func:`redact_stdout`): tokens
(``runners_token``…), emails, ``node_id``\\ s and every other field the
conformance suite never reads are dropped at record time, so a fixture
cannot carry a secret the protocol does not need.
"""


SECRET_KEY_RE = re.compile(r"(token|secret|password|private_key|api_key|node_id|email)", re.I)
"""Keys whose presence in a recorded answer means redaction failed."""


def redact_payload(value: object) -> object:
    """Keep only :data:`RECORDED_FIELDS` keys, recursively."""
    if isinstance(value, dict):
        return {k: redact_payload(v) for k, v in value.items() if k in RECORDED_FIELDS}
    if isinstance(value, list):
        return [redact_payload(v) for v in value]
    return value


def decode_json_values(text: str) -> list[object] | None:
    """Decode every top-level JSON value of ``text``; ``None`` when it is not JSON (a diff, a log)."""
    decoder = json.JSONDecoder()
    stripped = text.strip()
    values: list[object] = []
    pos = 0
    try:
        while pos < len(stripped):
            value, pos = decoder.raw_decode(stripped, pos)
            values.append(value)
            while pos < len(stripped) and stripped[pos].isspace():
                pos += 1
    except json.JSONDecodeError as exc:
        _log.debug("forge.recording.not_json", error=str(exc)[:120])
        return None
    return values or None


def redact_stdout(text: str) -> str:
    """Redact a recorded stdout: JSON answers keep only :data:`RECORDED_FIELDS`.

    One JSON value is re-serialised compactly; several (``--paginate``
    pages, ``--jq`` lines) one per line, which both decoders accept.
    Non-JSON output (diffs, logs, ``git`` output) is kept verbatim and
    left to :func:`find_secrets`.
    """
    values = decode_json_values(text)
    if values is None:
        return text
    return "\n".join(json.dumps(redact_payload(v), ensure_ascii=False) for v in values)


def find_secrets(text: str) -> list[str]:
    """Return every secret-shaped value or key found in ``text`` (and its JSON)."""
    found = [m.group(0) for pattern in SECRET_VALUE_PATTERNS for m in pattern.finditer(text)]
    for value in decode_json_values(text) or []:
        found += _secret_keys(value)
    return found


def _secret_keys(value: object) -> list[str]:
    """Keys matching :data:`SECRET_KEY_RE` anywhere in a decoded payload."""
    if isinstance(value, dict):
        hits = [k for k in value if SECRET_KEY_RE.search(k)]
        for v in value.values():
            hits += _secret_keys(v)
        return hits
    if isinstance(value, list):
        return [k for v in value for k in _secret_keys(v)]
    return []


@dataclass
class Exchange:
    """One recorded subprocess call.

    Attributes:
        program: ``gh``, ``glab`` or ``git`` (the binary's basename).
        args: Argv after the program.
        stdin: The stdin payload, when any.
        returncode: Exit code.
        stdout: Captured stdout.
        stderr: Captured stderr.
    """

    program: str
    args: list[str]
    stdin: str | None
    returncode: int
    stdout: str
    stderr: str

    def to_json(self) -> dict[str, Any]:
        """The JSON object stored in a fixture."""
        return {
            "program": self.program,
            "args": self.args,
            "stdin": self.stdin,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Exchange:
        """Rebuild an exchange from its fixture object."""
        return cls(
            program=str(data["program"]),
            args=[str(a) for a in data["args"]],
            stdin=data.get("stdin"),
            returncode=int(data["returncode"]),
            stdout=str(data.get("stdout") or ""),
            stderr=str(data.get("stderr") or ""),
        )


def _program(argv0: str) -> str:
    """The basename of a spawned binary."""
    return Path(argv0).name


@dataclass
class RecordingSpawn:
    """A ``_spawn`` replacement that forwards to the real one and records.

    Attributes:
        inner: The forge's original ``_spawn``.
        exchanges: Every call made so far, in order.
    """

    inner: Any
    exchanges: list[Exchange] = field(default_factory=list)

    def __call__(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        """Run ``argv`` for real and record the exchange."""
        res: CommandResult = self.inner(argv, input_data=input_data)
        self.exchanges.append(
            Exchange(
                program=_program(argv[0]),
                args=list(argv[1:]),
                stdin=input_data,
                returncode=res.returncode,
                stdout=res.stdout,
                stderr=res.stderr,
            )
        )
        return res


class ReplayMismatchError(AssertionError):
    """The code under test made a call the fixture did not record."""


@dataclass
class ReplaySpawn:
    """A ``_spawn`` replacement that answers from recorded exchanges, in order.

    Attributes:
        exchanges: The recorded exchanges still to be consumed.
    """

    exchanges: list[Exchange]

    def __call__(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        """Return the next recorded result after checking the argv matches."""
        if not self.exchanges:
            raise ReplayMismatchError(f"unrecorded call: {argv[1:]}")
        expected = self.exchanges.pop(0)
        actual = (_program(argv[0]), list(argv[1:]))
        if actual != (expected.program, expected.args):
            raise ReplayMismatchError(f"expected {expected.program} {expected.args}, got {actual}")
        if (input_data or None) != (expected.stdin or None):
            raise ReplayMismatchError(f"stdin differs for {expected.args}")
        return CommandResult(
            returncode=expected.returncode,
            stdout=expected.stdout,
            stderr=expected.stderr,
            argv=list(argv),
        )


def coerce_call(
    method: str, args: list[Any], kwargs: dict[str, Any]
) -> tuple[list[Any], dict[str, Any]]:
    """Turn a fixture's JSON arguments into the protocol's typed arguments.

    ``post_review`` takes a :class:`ReviewDraft`; ``merge`` a
    :class:`MergeMethod`. Every other method takes JSON scalars.
    """
    typed_args = list(args)
    typed_kwargs = dict(kwargs)
    if method == "post_review" and len(typed_args) == 2 and isinstance(typed_args[1], dict):
        draft = dict(typed_args[1])
        draft["comments"] = [InlineCommentDraft(**c) for c in draft.get("comments", [])]
        typed_args[1] = ReviewDraft(**draft)
    if method == "merge" and "method" in typed_kwargs:
        typed_kwargs["method"] = MergeMethod(typed_kwargs["method"])
    return typed_args, typed_kwargs


def serialize_result(value: object) -> object:
    """The JSON form of a protocol method's return value."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, CommandResult):
        return {"returncode": value.returncode, "stdout": value.stdout, "stderr": value.stderr}
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, list):
        return [serialize_result(v) for v in value]
    return value


def load_fixture(path: Path) -> dict[str, Any]:
    """Read one fixture file."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def dump_fixture(path: Path, data: dict[str, Any]) -> None:
    """Write one fixture file (sorted keys, two-space indent, trailing newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
