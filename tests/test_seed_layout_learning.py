from types import SimpleNamespace
from unittest.mock import AsyncMock

from qqfarm import catalog, wire
from qqfarm.config import Settings
from qqfarm.engine import FarmEngine
from qqfarm.web_service import FarmService


def land(identifier, *, master=0, seed=None, linked=(), width=1):
    fields = wire.uint(1, identifier) + wire.uint(2, 1) + wire.uint(3, 5)
    if master:
        fields += wire.uint(13, master)
    if seed:
        crop = wire.uint(1, 1000000 + seed) + wire.text(2, "活动植物")
        crop += wire.blob(4, wire.uint(1, 1) + wire.uint(2, 100))
        crop += wire.blob(4, wire.uint(1, 6) + wire.uint(2, 500))
        fields += wire.blob(10, crop) + wire.packed(14, linked) + wire.uint(15, width)
    return wire.blob(1, fields)


def inventory(seed, count):
    return wire.blob(1, wire.blob(1, wire.uint(1, seed) + wire.uint(2, count)))


def game(replies):
    return SimpleNamespace(
        gid=123,
        gold=1000,
        settings=Settings("test"),
        server_now=lambda: 100,
        rpc=AsyncMock(side_effect=replies),
    )


def log():
    return SimpleNamespace(record=lambda *args, **kwargs: None)


async def test_unknown_large_seed_replans_after_the_first_complete_reply():
    seed = next(iter(catalog.INFERRED_PLANTS))
    empty = b"".join(land(i) for i in range(1, 9))
    first = land(5, seed=seed, linked=[6, 1, 2], width=2)
    first += b"".join(land(i, master=5) for i in [6, 1, 2])
    second = land(7, seed=seed, linked=[8, 3, 4], width=2)
    second += b"".join(land(i, master=7) for i in [8, 3, 4])
    client = game([empty, inventory(seed, 2), first, second])
    engine = FarmEngine(
        Settings("test"), client, log(), apply=True, seed_priority=[seed], purchase_mode="inventory"
    )
    rows = await engine.round()
    requests = [
        wire.parse(wire.parse(call.args[2])[2][0])
        for call in client.rpc.call_args_list
        if call.args[1] == "Plant"
    ]
    assert list(requests[0][2][0]) == [5]
    assert list(requests[1][2][0]) == [7, 8, 3, 4]
    assert engine.seed_layouts[seed] == 2 and engine.inventory[seed] == 0
    assert all(plot.plant_id or plot.master_id for plot in rows.values())


async def test_unknown_single_seed_replans_as_single_cells_after_confirmation():
    seed = next(iter(catalog.INFERRED_PLANTS))
    empty = b"".join(land(i) for i in [5, 6, 1, 2])
    client = game([empty, inventory(seed, 2), land(5, seed=seed), land(6, seed=seed)])
    engine = FarmEngine(
        Settings("test"), client, log(), apply=True, seed_priority=[seed], purchase_mode="inventory"
    )
    await engine.round()
    assert engine.seed_layouts[seed] == 1
    assert [call.args[1] for call in client.rpc.call_args_list] == [
        "AllLands",
        "Bag",
        "Plant",
        "Plant",
    ]


async def test_unknown_seed_is_not_probed_without_space_for_a_possible_large_crop():
    seed = next(iter(catalog.INFERRED_PLANTS))
    client = game([land(1), inventory(seed, 1)])
    engine = FarmEngine(
        Settings("test"), client, log(), apply=True, seed_priority=[seed], purchase_mode="inventory"
    )
    await engine.round()
    assert [call.args[1] for call in client.rpc.call_args_list] == ["AllLands", "Bag"]
    assert seed not in engine.seed_layouts


def test_confirmed_event_layout_survives_restart_and_updates_the_plant_list(tmp_path):
    seed = next(iter(catalog.INFERRED_PLANTS))
    service = FarmService(tmp_path)
    service.client = SimpleNamespace(settings=Settings("test"))
    service.audit.record(
        "plant", "confirmed", seed=seed, land=5, occupied_lands=[5, 6, 1, 2], plot_size=2
    )
    service.audit.close()
    reopened = FarmService(tmp_path)
    try:
        assert reopened.seed_layouts[seed] == 2
        assert reopened.seed_layouts_version == Settings("test").client_version
        row = next(
            r
            for r in catalog.seeds(b"", {seed: 1}, layouts=reopened.seed_layouts)
            if r["seed_id"] == seed
        )
        assert row["plot_size"] == 2 and not row["layout_inferred"] and row["layout_verified"]
    finally:
        reopened.audit.close()
