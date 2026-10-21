"""API token checks for the inventory's admin endpoints."""

from __future__ import annotations

import hmac
import os


def admin_token() -> str:
    """Return the admin token from ``INVENTORY_ADMIN_TOKEN``.

    Raises:
        RuntimeError: The variable is unset or empty.
    """
    token = os.environ.get("INVENTORY_ADMIN_TOKEN", "")
    if not token:
        raise RuntimeError("INVENTORY_ADMIN_TOKEN is not set")
    return token


def is_admin(presented: str) -> bool:
    """Return True when ``presented`` is the admin token (constant-time compare)."""
    return hmac.compare_digest(presented.encode(), admin_token().encode())
