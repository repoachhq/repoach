from inventory.store import Item, Store


def test_save_then_get():
    store = Store()
    store.save(Item("p1", "pen", 150, 10))
    assert store.get("p1") == Item("p1", "pen", 150, 10)


def test_find_by_name_matches_fragment():
    store = Store()
    store.save(Item("p1", "blue pen", 150, 10))
    store.save(Item("p2", "pad", 300, 2))
    assert [item.sku for item in store.find_by_name("pen")] == ["p1"]
