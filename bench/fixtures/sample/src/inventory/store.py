"""SQLite-backed storage of inventory items."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    """One stocked item.

    Attributes:
        sku: Stock-keeping unit, unique.
        name: Display name.
        price_cents: Unit price in cents.
        quantity: Units in stock.
    """

    sku: str
    name: str
    price_cents: int
    quantity: int


class Store:
    """Items persisted in one SQLite database.

    Args:
        path: The database file, or ``:memory:``.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "sku TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "price_cents INTEGER NOT NULL, quantity INTEGER NOT NULL)"
        )

    def save(self, item: Item) -> None:
        """Insert or replace ``item``."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?)",
                (item.sku, item.name, item.price_cents, item.quantity),
            )

    def get(self, sku: str) -> Item | None:
        """Return the item with ``sku``, or None."""
        row = self._conn.execute(
            "SELECT sku, name, price_cents, quantity FROM items WHERE sku = ?", (sku,)
        ).fetchone()
        return Item(*row) if row else None

    def find_by_name(self, fragment: str) -> list[Item]:
        """Return the items whose name contains ``fragment``, by SKU."""
        rows = self._conn.execute(
            "SELECT sku, name, price_cents, quantity FROM items "
            "WHERE name LIKE ? ORDER BY sku",
            (f"%{fragment}%",),
        ).fetchall()
        return [Item(*row) for row in rows]

    def all(self) -> list[Item]:
        """Return every item, by SKU."""
        rows = self._conn.execute(
            "SELECT sku, name, price_cents, quantity FROM items ORDER BY sku"
        ).fetchall()
        return [Item(*row) for row in rows]
