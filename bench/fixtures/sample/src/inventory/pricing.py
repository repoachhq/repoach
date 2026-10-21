"""Prices, discounts and order totals, all in integer cents."""

from __future__ import annotations

from .store import Item


def apply_discount(price_cents: int, percent: int) -> int:
    """Return ``price_cents`` reduced by ``percent`` per cent, rounded down.

    Args:
        price_cents: The undiscounted price.
        percent: The discount, 0 to 100.

    Raises:
        ValueError: ``percent`` is outside 0..100.
    """
    if not 0 <= percent <= 100:
        raise ValueError(f"discount {percent}% outside 0..100")
    return price_cents * (100 - percent) // 100


def order_total(items: list[tuple[Item, int]]) -> int:
    """Return the total of ``(item, units)`` lines, in cents.

    Raises:
        ValueError: A line orders more units than are in stock.
    """
    total = 0
    for item, units in items:
        if units > item.quantity:
            raise ValueError(f"{item.sku}: {units} ordered, {item.quantity} in stock")
        total += item.price_cents * units
    return total
