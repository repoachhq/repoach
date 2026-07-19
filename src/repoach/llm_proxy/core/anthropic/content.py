"""Content block helpers for Anthropic-compatible payloads."""

from typing import Any


def get_block_attr(block: Any, attr: str, default: Any = None) -> Any:
    """Get an attribute from a Pydantic model, lightweight object, or dict."""
    if hasattr(block, attr):
        return getattr(block, attr)
    if isinstance(block, dict):
        return block.get(attr, default)
    return default


def get_block_type(block: Any) -> str | None:
    """Return a content block type when present."""
    return get_block_attr(block, "type")
