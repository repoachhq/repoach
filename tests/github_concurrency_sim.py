"""A simulator of GitHub Actions concurrency over a parsed workflow file.

SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF (A1, AC5). It replays a list of
events against one workflow and reports what each job did: ``completed``,
``skipped`` or ``cancelled`` (and whether it was cancelled while running).
It implements only the semantics the spec assumes, and nothing about
runners or steps:

* a concurrency group holds at most one running and one pending item; a
  new item replaces the pending one (which is cancelled); with
  ``cancel-in-progress`` true it also cancels the running one;
* a workflow-level ``concurrency`` is entered by every run as it is
  created, before any job ``if`` is evaluated;
* a job-level ``concurrency`` is entered only by a job whose ``if`` held:
  a job skipped by its ``if`` takes no slot;
* a job's ``if`` without a status function is ``success() && (<if>)``.

Expressions are evaluated on the fields they name: the job ``if`` reads
``github.event.comment.user.type`` (the comment's author) while
``cancel-in-progress: ${{ github.actor != 'github-actions[bot]' }}``
reads ``github.actor`` (who triggered the run). Each job takes
``durations[job_id]`` seconds (default 60) once it holds its slot.
"""

from __future__ import annotations

import heapq
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<str>'(?:[^']|'')*')|(?P<op>==|!=|&&|\|\||<=|>=|[!<>().,\[\]])"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_-]*))"
)
_EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_STATUS_FUNCTIONS = ("success(", "failure(", "cancelled(", "always(")


class ExpressionError(ValueError):
    """An expression the simulator cannot parse."""


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        match = _TOKEN_RE.match(text, pos)
        if match is None or match.end() == pos:
            raise ExpressionError(f"cannot tokenize {text[pos : pos + 20]!r}")
        kind = match.lastgroup or ""
        out.append((kind, match.group(kind)))
        pos = match.end()
    return out


def truthy(value: object) -> bool:
    """GitHub's truthiness: null, false, 0 and '' are false."""
    return value not in (None, False, 0, "")


def _equal(left: object, right: object) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    return left == right


def _contains(search: object, item: object) -> bool:
    if isinstance(search, list):
        return any(_equal(element, item) for element in search)
    if isinstance(search, str) and item is not None:
        return str(item).lower() in search.lower()
    return False


@dataclass
class Context:
    """What an expression can read.

    Attributes:
        github: The ``github`` context (``event_name``, ``actor``, ``event``…).
        vars: Repository variables.
        needs: ``{job_id: {"result": str, "outputs": {...}}}``.
        inputs: Dispatch inputs.
        run_cancelled: Value of ``cancelled()``.
    """

    github: Mapping[str, Any]
    vars: Mapping[str, Any] = field(default_factory=dict)
    needs: Mapping[str, Any] = field(default_factory=dict)
    inputs: Mapping[str, Any] = field(default_factory=dict)
    run_cancelled: bool = False


class _Parser:
    def __init__(self, text: str, ctx: Context) -> None:
        self.tokens = _tokens(text)
        self.pos = 0
        self.ctx = ctx

    def peek(self) -> str | None:
        return self.tokens[self.pos][1] if self.pos < len(self.tokens) else None

    def take(self, expected: str | None = None) -> tuple[str, str]:
        if self.pos >= len(self.tokens):
            raise ExpressionError("unexpected end of expression")
        token = self.tokens[self.pos]
        if expected is not None and token[1] != expected:
            raise ExpressionError(f"expected {expected!r}, got {token[1]!r}")
        self.pos += 1
        return token

    def parse(self) -> object:
        value = self.or_()
        if self.pos != len(self.tokens):
            raise ExpressionError(f"trailing tokens from {self.tokens[self.pos][1]!r}")
        return value

    def or_(self) -> object:
        value = self.and_()
        while self.peek() == "||":
            self.take()
            right = self.and_()
            value = value if truthy(value) else right
        return value

    def and_(self) -> object:
        value = self.eq()
        while self.peek() == "&&":
            self.take()
            right = self.eq()
            value = right if truthy(value) else value
        return value

    def eq(self) -> object:
        value = self.unary()
        while self.peek() in {"==", "!="}:
            op = self.take()[1]
            right = self.unary()
            value = _equal(value, right) if op == "==" else not _equal(value, right)
        return value

    def unary(self) -> object:
        if self.peek() == "!":
            self.take()
            return not truthy(self.unary())
        return self.postfix()

    def postfix(self) -> object:
        value = self.primary()
        while self.peek() in {".", "["}:
            if self.take()[1] == ".":
                key: object = self.take()[1]
            else:
                key = self.or_()
                self.take("]")
            value = value.get(str(key)) if isinstance(value, Mapping) else None
        return value

    def primary(self) -> object:
        kind, text = self.take()
        if kind == "num":
            return float(text) if "." in text else int(text)
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if text == "(":
            value = self.or_()
            self.take(")")
            return value
        if kind != "name":
            raise ExpressionError(f"unexpected {text!r}")
        if text in {"true", "false"}:
            return text == "true"
        if text == "null":
            return None
        if self.peek() == "(":
            self.take()
            args: list[object] = []
            while self.peek() != ")":
                args.append(self.or_())
                if self.peek() == ",":
                    self.take()
            self.take(")")
            return self.call(text, args)
        contexts: dict[str, object] = {
            "github": self.ctx.github,
            "vars": self.ctx.vars,
            "needs": self.ctx.needs,
            "inputs": self.ctx.inputs,
        }
        return contexts.get(text)

    def call(self, name: str, args: Sequence[object]) -> object:
        lowered = name.lower()
        if lowered == "contains":
            return _contains(args[0], args[1])
        if lowered == "fromjson":
            return json.loads(str(args[0]))
        if lowered == "always":
            return True
        if lowered == "cancelled":
            return self.ctx.run_cancelled
        if lowered == "success":
            return not self.ctx.run_cancelled and all(
                isinstance(n, Mapping) and n.get("result") == "success"
                for n in self.ctx.needs.values()
            )
        if lowered == "failure":
            return any(
                isinstance(n, Mapping) and n.get("result") == "failure"
                for n in self.ctx.needs.values()
            )
        raise ExpressionError(f"unsupported function {name}()")


def evaluate(expression: str, ctx: Context) -> object:
    """Evaluate one expression (without ``${{ }}``)."""
    return _Parser(expression, ctx).parse()


def render(value: object, ctx: Context) -> object:
    """Evaluate a workflow value: a lone ``${{ }}`` keeps its type, else string interpolation."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    whole = _EXPR_RE.fullmatch(stripped)
    if whole is not None:
        return evaluate(whole.group(1), ctx)

    def text_of(match: re.Match[str]) -> str:
        result = evaluate(match.group(1), ctx)
        if isinstance(result, bool):
            return "true" if result else "false"
        if isinstance(result, float) and result.is_integer():
            return str(int(result))
        return "" if result is None else str(result)

    return _EXPR_RE.sub(text_of, value)


def job_condition(raw: object, ctx: Context) -> bool:
    """Evaluate a job ``if``; without a status function it is ``success() && (<if>)``."""
    if raw is None:
        return bool(evaluate("success()", ctx))
    text = str(raw).strip()
    whole = _EXPR_RE.fullmatch(text)
    if whole is not None:
        text = whole.group(1)
    if not any(fn in text.replace(" ", "") for fn in _STATUS_FUNCTIONS):
        text = f"success() && ({text})"
    return truthy(evaluate(text, ctx))


@dataclass
class SimEvent:
    """One triggering event.

    Attributes:
        at: Seconds from the start of the simulation.
        event_name: ``pull_request_review``, ``issue_comment``…
        actor: ``github.actor``: who triggered the run.
        payload: ``github.event``.
    """

    at: float
    event_name: str
    actor: str
    payload: Mapping[str, Any]


@dataclass
class JobRecord:
    """What one job of one run did."""

    run: int
    job: str
    result: str = "waiting"
    started: float | None = None
    ended: float | None = None
    cancelled_running: bool = False


@dataclass(eq=False)
class _Run:
    index: int
    event: SimEvent
    status: str = "created"
    jobs: dict[str, JobRecord] = field(default_factory=dict)
    cancelled: bool = False


@dataclass
class _Group:
    running: Any = None
    pending: Any = None


class Simulator:
    """Replay events against one workflow under GitHub's concurrency rules."""

    def __init__(
        self,
        workflow: Mapping[str, Any],
        *,
        durations: Mapping[str, float] | None = None,
        outputs: Mapping[str, Mapping[str, str]] | None = None,
        variables: Mapping[str, str] | None = None,
        repository: str = "repoachhq/repoach",
    ) -> None:
        self.workflow = workflow
        self.jobs: Mapping[str, Mapping[str, Any]] = workflow.get("jobs") or {}
        self.durations = dict(durations or {})
        self.outputs = {k: dict(v) for k, v in (outputs or {}).items()}
        self.variables = dict(variables or {})
        self.repository = repository
        self.groups: dict[str, _Group] = {}
        self.runs: list[_Run] = []
        self.queue: list[tuple[float, int, str, Any]] = []
        self.seq = 0
        self.now = 0.0

    @classmethod
    def from_file(cls, path: Path, **kwargs: Any) -> Simulator:
        """Build a simulator over a workflow file."""
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")), **kwargs)

    def _push(self, at: float, kind: str, item: Any) -> None:
        self.seq += 1
        heapq.heappush(self.queue, (at, self.seq, kind, item))

    def _ctx(self, run: _Run, job_id: str | None = None) -> Context:
        needs: dict[str, Any] = {}
        if job_id is not None:
            for need in _needs_of(self.jobs[job_id]):
                record = run.jobs[need]
                needs[need] = {
                    "result": record.result,
                    "outputs": self.outputs.get(need, {}) if record.result == "success" else {},
                }
        event = run.event
        return Context(
            github={
                "event_name": event.event_name,
                "actor": event.actor,
                "event": event.payload,
                "repository": self.repository,
            },
            vars=self.variables,
            needs=needs,
            inputs=event.payload.get("inputs") or {},
            run_cancelled=run.cancelled,
        )

    def run(self, events: Sequence[SimEvent]) -> list[JobRecord]:
        """Replay ``events``; return every job record, in run then job order."""
        for event in events:
            self._push(event.at, "trigger", event)
        while self.queue:
            at, _, kind, item = heapq.heappop(self.queue)
            self.now = at
            if kind == "trigger":
                self._trigger(item)
            elif kind == "finish":
                run, job_id = item
                record = run.jobs[job_id]
                if record.result == "running":
                    record.result = "success"
                    record.ended = at
                    self._release_job(run, job_id)
                    self._advance(run)
        return [record for run in self.runs for record in run.jobs.values()]

    def _trigger(self, event: SimEvent) -> None:
        run = _Run(index=len(self.runs), event=event)
        run.jobs = {job_id: JobRecord(run=run.index, job=job_id) for job_id in self.jobs}
        self.runs.append(run)
        concurrency = self.workflow.get("concurrency")
        if concurrency is None:
            self._start_run(run)
            return
        group, cancel = self._concurrency(concurrency, self._ctx(run))
        self._enter(group, ("run", run), cancel)

    def _concurrency(self, spec: object, ctx: Context) -> tuple[str, bool]:
        if isinstance(spec, str):
            return str(render(spec, ctx)), False
        assert isinstance(spec, Mapping)
        group = str(render(spec.get("group", ""), ctx)).strip()
        cancel = truthy(render(spec.get("cancel-in-progress", False), ctx))
        return group, cancel

    def _enter(self, group_name: str, item: tuple[str, Any], cancel: bool) -> None:
        group = self.groups.setdefault(group_name, _Group())
        if group.pending is not None:
            self._cancel_item(group.pending, running=False)
            group.pending = None
        if group.running is None:
            group.running = item
            self._start_item(item)
            return
        if cancel:
            running = group.running
            group.running = item
            self._cancel_item(running, running=True)
            self._start_item(item)
            return
        group.pending = item

    def _release(self, group_name: str, item: tuple[str, Any]) -> None:
        group = self.groups.get(group_name)
        if group is None or group.running is not item:
            return
        group.running = group.pending
        group.pending = None
        if group.running is not None:
            self._start_item(group.running)

    def _start_item(self, item: tuple[str, Any]) -> None:
        if item[0] == "run":
            self._start_run(item[1])
        else:
            run, job_id = item[1]
            record = run.jobs[job_id]
            record.result = "running"
            record.started = self.now
            self._push(self.now + self.durations.get(job_id, 60.0), "finish", (run, job_id))

    def _cancel_item(self, item: tuple[str, Any], *, running: bool) -> None:
        if item[0] == "run":
            self._cancel_run(item[1])
            return
        run, job_id = item[1]
        record = run.jobs[job_id]
        record.cancelled_running = record.result == "running"
        record.result = "cancelled"
        record.ended = self.now
        self._advance(run)

    def _start_run(self, run: _Run) -> None:
        run.status = "running"
        self._advance(run)

    def _cancel_run(self, run: _Run) -> None:
        run.cancelled = True
        for record in run.jobs.values():
            if record.result in {"running", "queued", "waiting"}:
                record.cancelled_running = record.result == "running"
                record.result = "cancelled"
                record.ended = self.now
        for group in self.groups.values():
            for slot in ("running", "pending"):
                item = getattr(group, slot)
                if item is not None and item[0] == "job" and item[1][0] is run:
                    setattr(group, slot, None)
                    if slot == "running" and group.pending is not None:
                        group.running, group.pending = group.pending, None
                        self._start_item(group.running)
        run.status = "completed"

    def _release_job(self, run: _Run, job_id: str) -> None:
        for name, group in self.groups.items():
            item = group.running
            if item is not None and item[0] == "job" and item[1][0] is run and item[1][1] == job_id:
                self._release(name, item)
                return

    def _advance(self, run: _Run) -> None:
        if run.status != "running":
            return
        progressed = True
        while progressed:
            progressed = False
            for job_id, job in self.jobs.items():
                record = run.jobs[job_id]
                if record.result != "waiting":
                    continue
                needs = _needs_of(job)
                if any(run.jobs[n].result in {"waiting", "queued", "running"} for n in needs):
                    continue
                ctx = self._ctx(run, job_id)
                if not job_condition(job.get("if"), ctx):
                    record.result = "skipped"
                    progressed = True
                    continue
                record.result = "queued"
                concurrency = job.get("concurrency")
                if concurrency is None:
                    self._start_item(("job", (run, job_id)))
                else:
                    group, cancel = self._concurrency(concurrency, ctx)
                    self._enter(group, ("job", (run, job_id)), cancel)
                progressed = True
        if all(r.result not in {"waiting", "queued", "running"} for r in run.jobs.values()):
            run.status = "completed"
            concurrency = self.workflow.get("concurrency")
            if concurrency is not None:
                group, _ = self._concurrency(concurrency, self._ctx(run))
                self._release(group, self._run_item(group, run))

    def _run_item(self, group_name: str, run: _Run) -> tuple[str, Any] | None:
        group = self.groups.get(group_name)
        if group is not None and group.running is not None and group.running[1] is run:
            return group.running
        return None


def _needs_of(job: Mapping[str, Any]) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def review_event(at: float, pr: int, *, actor: str = "jwfaye", user_type: str = "User") -> SimEvent:
    """A ``pull_request_review`` event on ``pr`` (the review author is ``user_type``)."""
    return SimEvent(
        at=at,
        event_name="pull_request_review",
        actor=actor,
        payload={
            "action": "submitted",
            "pull_request": {"number": pr, "draft": False, "head": {"ref": f"feat/pr-{pr}"}},
            "review": {"user": {"login": actor, "type": user_type}},
        },
    )


def comment_event(
    at: float,
    pr: int,
    *,
    actor: str = "jwfaye",
    author: str = "github-actions[bot]",
    author_type: str = "Bot",
    action: str = "edited",
) -> SimEvent:
    """An ``issue_comment`` event on ``pr``: ``author`` wrote the comment, ``actor`` triggered it."""
    return SimEvent(
        at=at,
        event_name="issue_comment",
        actor=actor,
        payload={
            "action": action,
            "issue": {"number": pr, "pull_request": {"url": f"https://example/pull/{pr}"}},
            "comment": {"user": {"login": author, "type": author_type}},
        },
    )


__all__ = [
    "Context",
    "ExpressionError",
    "JobRecord",
    "SimEvent",
    "Simulator",
    "comment_event",
    "evaluate",
    "job_condition",
    "render",
    "review_event",
    "truthy",
]
