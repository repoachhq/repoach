"""SP-CHAINPILOT-CHAIN-REWRITE — the mechanical chains.env rewriter (Phase 3d-1a).

Pure, text-in / text-out. Given the ``chains.env`` content and a list of
structural edits, this re-renders ONLY the four ``MODEL_*`` slot lines and
returns the new content, leaving every comment, blank line, other key and
ordering byte-for-byte verbatim.

It is the *hands* of the apply layer: it knows how to reshape a chain safely —
the ``claude_code`` backstop is never removed or moved, a chain is never
emptied, ``demote``/``promote`` shift one step within the mutable prefix above
the backstop, ``insert`` adds above the backstop de-duplicated — but it holds NO
policy. Which edits to make is the decision engine (3b) plus the placement
classifier (3d-1b); whether to write the result to disk is 3d-2.

An edit that would breach a safety invariant, or that finds nothing to change,
is recorded in :attr:`RewriteResult.skipped` with a reason rather than forced.
Only the slots that actually change are re-rendered, so untouched slots stay
identical to the byte.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ferova.llm_proxy.routing.chain import Chain
from ferova.llm_proxy.routing.refs import ModelRef

_TIERS = ("opus", "sonnet", "haiku")
_SLOT_PREFIX = "MODEL_"
_BACKSTOP_PROVIDER = "claude_code"


class EditOp(StrEnum):
    """The kind of structural edit applied to a chain."""

    EVICT_MODEL = "evict_model"
    DROP_PROVIDER = "drop_provider"
    DEMOTE = "demote"
    PROMOTE = "promote"
    INSERT = "insert"


@dataclass(frozen=True)
class ChainEdit:
    """One structural edit request against the chains.

    Attributes:
        op: The kind of edit.
        model: The target model segment — matched across providers for
            ``evict_model`` / ``demote`` / ``promote``; combined with
            ``provider`` to identify the exact ref for ``drop_provider`` /
            ``insert``.
        provider: The provider id; required for ``drop_provider`` and
            ``insert``, ignored otherwise.
        tier: The slot to scope the edit to (one of ``opus`` / ``sonnet`` /
            ``haiku``). ``insert`` requires it; for the others
            ``None`` means every slot the model appears in.
        position: For ``insert``, the 0-based index to insert at (clamped into
            the mutable prefix above the backstop); ignored otherwise.
        reason: Human-readable justification, carried through for journaling.
    """

    op: EditOp
    model: str
    provider: str | None = None
    tier: str | None = None
    position: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class SkippedEdit:
    """An edit that was refused or found nothing to change, with the reason."""

    edit: ChainEdit
    reason: str


@dataclass(frozen=True)
class RewriteResult:
    """The outcome of :func:`rewrite_chains`.

    Attributes:
        new_content: The rewritten ``chains.env`` text (untouched slots and all
            non-slot lines are byte-identical to the input).
        applied: The edits that changed at least one slot, in the order applied.
            This lists the mutation *steps* taken, not a net diff against the
            input: a pair of edits that cancel (a demote then a matching
            promote) appears here even though ``new_content`` is unchanged.
        skipped: One :class:`SkippedEdit` per edit with at least one refused or
            no-op slot. A tier-agnostic edit that changes one slot but is
            refused on another appears in BOTH ``applied`` and ``skipped`` so no
            per-slot refusal reason is lost.
    """

    new_content: str
    applied: tuple[ChainEdit, ...]
    skipped: tuple[SkippedEdit, ...]


def _is_backstop(ref: ModelRef) -> bool:
    """Whether a ref is the immovable ``claude_code`` subprocess backstop."""
    return bool(ref.provider_id == _BACKSTOP_PROVIDER)


def _slot_key(tier: str) -> str:
    """The ``chains.env`` key for a tier (``opus`` -> ``MODEL_OPUS``)."""
    return f"{_SLOT_PREFIX}{tier.upper()}"


def _evict(refs: list[ModelRef], model: str) -> tuple[bool, str]:
    """Remove every ref of ``model`` from a slot, guarding backstop + emptiness."""
    removed = [ref for ref in refs if ref.model == model]
    if not removed:
        return False, "model not present"
    if any(_is_backstop(ref) for ref in removed):
        return False, "refuses to evict the claude_code backstop"
    keep = [ref for ref in refs if ref.model != model]
    if not keep:
        return False, "would leave the chain empty"
    refs[:] = keep
    return True, ""


def _drop_provider(refs: list[ModelRef], model: str, provider: str | None) -> tuple[bool, str]:
    """Remove the single ``(provider, model)`` ref from a slot."""
    if provider is None:
        return False, "drop_provider needs a provider"
    index = next(
        (i for i, ref in enumerate(refs) if ref.model == model and ref.provider_id == provider),
        None,
    )
    if index is None:
        return False, "ref not present"
    if _is_backstop(refs[index]):
        return False, "refuses to drop the claude_code backstop"
    if len(refs) == 1:
        return False, "would leave the chain empty"
    del refs[index]
    return True, ""


def _shift(refs: list[ModelRef], model: str, *, up: bool) -> tuple[bool, str]:
    """Move the first ref of ``model`` one step, never past the backstop."""
    index = next((i for i, ref in enumerate(refs) if ref.model == model), None)
    if index is None:
        return False, "model not present"
    if _is_backstop(refs[index]):
        return False, "refuses to move the claude_code backstop"
    if up:
        target = index - 1
        if target < 0:
            return False, "already at the head"
        if _is_backstop(refs[target]):
            return False, "refuses to move past the claude_code backstop"
    else:
        target = index + 1
        if target >= len(refs) or _is_backstop(refs[target]):
            return False, "already last above the backstop"
    refs[index], refs[target] = refs[target], refs[index]
    return True, ""


def _insert(refs: list[ModelRef], edit: ChainEdit) -> tuple[bool, str]:
    """Insert a new ref above the backstop, de-duplicated and provider-validated."""
    if edit.provider is None:
        return False, "insert needs a provider"
    try:
        new_ref = ModelRef.parse(f"{edit.provider}/{edit.model}")
    except ValueError as error:
        return False, str(error)
    if _is_backstop(new_ref):
        return False, "refuses to insert a claude_code backstop"
    if any(str(ref) == str(new_ref) for ref in refs):
        return False, "ref already present"
    backstop_at = next((i for i, ref in enumerate(refs) if _is_backstop(ref)), len(refs))
    position = backstop_at if edit.position is None else edit.position
    position = max(0, min(position, backstop_at))
    refs.insert(position, new_ref)
    return True, ""


def _target_tiers(chains: dict[str, list[ModelRef]], edit: ChainEdit) -> list[str]:
    """The slots an edit applies to: its explicit tier, else every slot with the model."""
    if edit.tier is not None:
        return [edit.tier] if edit.tier in chains else []
    return [tier for tier, refs in chains.items() if any(ref.model == edit.model for ref in refs)]


def _apply_edit(chains: dict[str, list[ModelRef]], edit: ChainEdit) -> tuple[bool, tuple[str, ...]]:
    """Apply one edit across its target slots.

    Returns ``(changed, refusals)`` where ``changed`` is whether any slot was
    mutated and ``refusals`` is one ``"<tier>: <reason>"`` per slot that refused
    or found nothing to change. A tier-agnostic edit can both change one slot and
    refuse another, so the two are reported independently (no per-slot reason is
    dropped, even on partial success).
    """
    if edit.op is EditOp.INSERT:
        if edit.tier is None:
            return False, ("insert needs a tier",)
        if edit.tier not in chains:
            return False, (f"unknown tier {edit.tier!r}",)
        ok, reason = _insert(chains[edit.tier], edit)
        return ok, () if ok else (reason,)
    if edit.tier is not None and edit.tier not in chains:
        return False, (f"unknown tier {edit.tier!r}",)
    tiers = _target_tiers(chains, edit)
    if not tiers:
        return False, ("model not present in any targeted slot",)
    changed = False
    refusals: list[str] = []
    for tier in tiers:
        refs = chains[tier]
        if edit.op is EditOp.EVICT_MODEL:
            ok, reason = _evict(refs, edit.model)
        elif edit.op is EditOp.DROP_PROVIDER:
            ok, reason = _drop_provider(refs, edit.model, edit.provider)
        elif edit.op is EditOp.DEMOTE:
            ok, reason = _shift(refs, edit.model, up=False)
        else:
            ok, reason = _shift(refs, edit.model, up=True)
        if ok:
            changed = True
        else:
            refusals.append(f"{tier}: {reason}")
    return changed, tuple(refusals)


def rewrite_chains(content: str, edits: Sequence[ChainEdit]) -> RewriteResult:
    """Apply structural edits to the chains, re-rendering only what changed.

    Parses the four ``MODEL_*`` slot lines, applies ``edits`` in order, and
    re-renders only the slots whose ref list actually changed. Every other line
    — comments, blank lines, other keys — is preserved verbatim, as is the
    trailing newline.

    Args:
        content: The full ``chains.env`` text.
        edits: The structural edits to apply, in order.

    Returns:
        The :class:`RewriteResult` with the new text, the applied edits and a
        reason for every skipped one.
    """
    lines = content.split("\n")
    wanted = {_slot_key(tier): tier for tier in _TIERS}
    chains: dict[str, list[ModelRef]] = {}
    line_of: dict[str, int] = {}
    for index, line in enumerate(lines):
        key, separator, value = line.partition("=")
        if separator and key in wanted:
            tier = wanted[key]
            chains[tier] = list(Chain.parse(value).refs)
            line_of[tier] = index
    original = {tier: tuple(refs) for tier, refs in chains.items()}

    applied: list[ChainEdit] = []
    skipped: list[SkippedEdit] = []
    for edit in edits:
        changed, refusals = _apply_edit(chains, edit)
        if changed:
            applied.append(edit)
        if refusals:
            skipped.append(SkippedEdit(edit=edit, reason="; ".join(refusals)))

    for tier, refs in chains.items():
        if tuple(refs) == original[tier]:
            continue
        rendered = ",".join(str(ref) for ref in refs)
        lines[line_of[tier]] = f"{_slot_key(tier)}={rendered}"

    return RewriteResult(
        new_content="\n".join(lines),
        applied=tuple(applied),
        skipped=tuple(skipped),
    )
