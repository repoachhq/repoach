import pytest

from inventory.pricing import apply_discount, order_total
from inventory.store import Item


def test_apply_discount_rounds_down():
    assert apply_discount(999, 10) == 899


def test_apply_discount_rejects_out_of_range():
    with pytest.raises(ValueError):
        apply_discount(100, 101)


def test_order_total_sums_lines():
    pen = Item("p1", "pen", 150, 10)
    pad = Item("p2", "pad", 300, 2)
    assert order_total([(pen, 2), (pad, 1)]) == 600


def test_order_total_refuses_more_than_stock():
    pad = Item("p2", "pad", 300, 2)
    with pytest.raises(ValueError):
        order_total([(pad, 3)])
