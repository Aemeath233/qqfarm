"""静态名称只作展示；解锁、价格、库存和成熟时间以服务端为准。"""

import json
from importlib.resources import files

from qqfarm import models, wire

DATA = json.loads(
    files("qqfarm").joinpath("resources/plant_catalog.json").read_text(encoding="utf-8")
)
PLANTS = {p["seed_id"]: p for p in DATA["plants"]}
ITEMS = {p["id"]: p for p in DATA["items"]}
OBSERVED = json.loads(
    files("qqfarm").joinpath("resources/observed_plants.json").read_text(encoding="utf-8")
)
PLANTS.update({p["seed_id"]: p for p in OBSERVED["plants"]})
ITEMS.update({p["id"]: p for p in OBSERVED["items"]})

# Some event seeds remain in inventory after their shop entry disappears. Their
# item IDs and matching fruit IDs use the regular seed/fruit ranges.
INFERRED_PLANTS = {}
for seed_id, item in ITEMS.items():
    fruit = ITEMS.get(seed_id + 20_000, {})
    if (
        20_000 <= seed_id < 30_000
        and item.get("type") == 5
        and seed_id not in PLANTS
        and fruit.get("type") == 6
    ):
        INFERRED_PLANTS[seed_id] = {
            "seed_id": seed_id,
            "plant_id": 1_000_000 + seed_id,
            "fruit_id": seed_id + 20_000,
            "name": item["name"].removesuffix("种子"),
            "land_level_need": fruit.get("level", 0),
            "plot_size": 1,
            "inventory_only": True,
            "layout_inferred": True,
        }


def plant(seed_id):
    """Return static metadata or conservative metadata inferred from a seed item."""
    return PLANTS.get(seed_id, INFERRED_PLANTS.get(seed_id, {}))


def inventory(payload):
    fields = wire.parse(wire.data(wire.parse(payload), 1))
    rows = []
    for value in fields.get(1, []):
        f = wire.parse(value)
        item_id, count = models.item(value)
        uid = wire.number(f, 6)
        meta = ITEMS.get(item_id, {})
        rows.append(
            {
                "id": item_id,
                "count": count,
                "uid": uid,
                "name": meta.get("name", f"物品 {item_id}"),
                "type": meta.get("type", 0),
                "sellable": meta.get("type") == 6 and meta.get("sell_verified", True),
                "base_sell_price": meta.get("base_sell_price", 0),
            }
        )
    return rows


def seeds(payload, bag=None, *, layouts=None):
    bag = bag or {}
    rows = []
    for value in wire.parse(payload).get(1, []):
        f = wire.parse(value)
        seed_id = wire.number(f, 6)
        if not 20000 <= seed_id < 30000:
            continue
        meta = plant(seed_id)
        conditions = [wire.parse(v) for v in f.get(8, [])]
        required = max((wire.number(c, 2) for c in conditions if wire.number(c, 1) == 1), default=0)
        rows.append(
            {
                **meta,
                "seed_id": seed_id,
                "goods_id": wire.number(f, 1),
                "name": meta.get("name", ITEMS.get(seed_id, {}).get("name", f"种子 {seed_id}")),
                "price": wire.number(f, 3),
                "pack_count": wire.number(f, 7),
                "unlocked": bool(wire.number(f, 5)),
                "required_level": required,
                "limit": wire.number(f, 4),
                "bought": wire.number(f, 2),
                "inventory": bag.get(seed_id, 0),
                "plot_size": meta.get("plot_size", 1),
                "inventory_only": False,
            }
        )
    offered = {row["seed_id"] for row in rows}
    for seed_id, meta in ({**PLANTS, **INFERRED_PLANTS}).items():
        if seed_id not in offered and meta.get("inventory_only"):
            rows.append(
                {
                    **meta,
                    "goods_id": 0,
                    "price": 0,
                    "pack_count": 1,
                    "unlocked": bag.get(seed_id, 0) > 0,
                    "required_level": meta.get("land_level_need", 0),
                    "limit": 0,
                    "bought": 0,
                    "inventory": bag.get(seed_id, 0),
                }
            )
    for row in rows:
        size = (layouts or {}).get(row["seed_id"])
        if row.get("layout_inferred") and size in {1, 2}:
            row.update(plot_size=size, layout_inferred=False, layout_verified=True)
    return rows


def tasks(payload, *, nested=True):
    info = wire.parse(wire.data(wire.parse(payload), 1)) if nested else wire.parse(payload)
    rows = []
    for group in (1, 2, 3):
        for value in info.get(group, []):
            f = wire.parse(value)
            required, progress = wire.number(f, 6), wire.number(f, 2)
            rows.append(
                {
                    "id": wire.number(f, 1),
                    "description": wire.string(f, 9)[:300],
                    "progress": progress,
                    "total": required,
                    "claimed": bool(wire.number(f, 3)),
                    "unlocked": bool(wire.number(f, 4)),
                    "type": wire.number(f, 10),
                    "claimable": bool(
                        required > 0
                        and progress >= required
                        and wire.number(f, 4)
                        and not wire.number(f, 3)
                    ),
                    "rewards": [
                        {
                            "id": models.item(v)[0],
                            "count": models.item(v)[1],
                            "name": ITEMS.get(models.item(v)[0], {}).get("name", "奖励"),
                        }
                        for v in f.get(5, [])
                    ],
                }
            )
    return rows


def friends(payload):
    rows = []
    for value in wire.parse(payload).get(1, []):
        f = wire.parse(value)
        brief = wire.parse(wire.data(f, 9))
        rows.append(
            {
                "id": wire.number(f, 1),
                "name": wire.string(f, 3)[:100],
                "remark": wire.string(f, 5)[:100],
                "level": wire.number(f, 6),
                "stealable_plots": wire.number(brief, 6),
                "dry_plots": wire.number(brief, 7),
                "weed_plots": wire.number(brief, 8),
                "bug_plots": wire.number(brief, 9),
            }
        )
    return rows


def limits(payload, tag=2):
    result = {}
    for value in wire.parse(payload).get(tag, []):
        f = wire.parse(value)
        result[wire.number(f, 1)] = {"used": wire.number(f, 2), "maximum": wire.number(f, 3)}
    return result
