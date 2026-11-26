from amir.baro.names import ItemNameIndex
from amir.worldstate.official import parse_void_trader
from amir.worldstate.wfcd import parse_inventory_names, parse_item_names


def _real(captured):
    visit = parse_void_trader(captured("official_worldstate_baro_present.json"))
    db = parse_item_names(captured("wfcd_items_names.json"))
    wfcd = parse_inventory_names(captured("wfcd_pc_baro_present.json"))
    return visit, ItemNameIndex(db), wfcd


def test_real_stock_names(captured):
    visit, index, wfcd = _real(captured)
    assert len(visit.manifest) == 56
    steps = {e.item_type: index.resolve_step(e.item_type, wfcd) for e in visit.manifest}
    assert sum(1 for s in steps.values() if s in (1, 2)) == 54
    plush = "/Lotus/StoreItems/Types/Items/ShipDecos/Plushies/PlushyBaroEvil"
    assert index.resolve(plush, wfcd) == "Eskhatos Ki'Teer Floof"
    box = next(t for t in steps if t.endswith("BaroTreasureBox"))
    assert steps[box] == 3
    assert index.resolve(box, wfcd) == "Baro Treasure Box"


def test_step4_fallback():
    index = ItemNameIndex({})
    item = "/Lotus/StoreItems/Types/BaroTreasureBox"
    assert index.resolve_step(item, {}) == 4
    assert index.resolve(item, {}) == "Baro Treasure Box"


def _unique_tail_entry(db):
    tails = [u.rsplit("/", 1)[-1] for u in db]
    for unique_name, name in db.items():
        if unique_name.startswith("/Lotus/") and tails.count(unique_name.rsplit("/", 1)[-1]) == 1:
            return unique_name, name
    raise AssertionError("no unique-tail entry in real database")


def test_step1_exact_and_step2_unique_tail(captured):
    db = parse_item_names(captured("wfcd_items_names.json"))
    unique_name, name = _unique_tail_entry(db)
    index = ItemNameIndex(db)
    tail = unique_name.rsplit("/", 1)[-1]
    store_path = unique_name.replace("/Lotus/", "/Lotus/StoreItems/", 1)
    assert index.resolve_step(store_path, {}) == 1
    moved = f"/Lotus/StoreItems/Moved/Elsewhere/{tail}"
    assert index.resolve_step(moved, {}) == 2
    assert index.resolve(moved, {}) == name


def test_ambiguous_tail_excluded_from_step2(captured):
    db = parse_item_names(captured("wfcd_items_names.json"))
    unique_name, name = _unique_tail_entry(db)
    other_name = next(n for n in db.values() if n != name)
    tail = unique_name.rsplit("/", 1)[-1]
    index = ItemNameIndex({**db, f"/Lotus/Moved/Elsewhere/{tail}": other_name})
    assert index.resolve_step(f"/Lotus/StoreItems/Other/Place/{tail}", {}) == 4
