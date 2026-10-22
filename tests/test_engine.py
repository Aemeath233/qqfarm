from dataclasses import replace

import pytest

from qqfarm import models, wire
from qqfarm.client import UncertainOutcome
from qqfarm.config import FarmError, Settings
from qqfarm.engine import FarmEngine


def plot(identifier, phases=(), *, unlocked=True, shared=False, master=0):
    fields = wire.uint(1, identifier) + wire.uint(2, int(unlocked))
    fields += wire.uint(11, int(shared)) + wire.uint(13, master)
    if phases:
        plant = wire.uint(1, 1020002) + wire.text(2, "白萝卜")
        for phase, begin in phases:
            plant += wire.blob(4, wire.uint(1, phase) + wire.uint(2, begin))
        fields += wire.blob(10, plant)
    return wire.blob(1, fields)


def inventory(count):
    return wire.blob(1, wire.blob(1, wire.uint(1, 20002) + wire.uint(2, count)))


class Log:
    def __init__(self):
        self.rows = []

    def record(self, operation, state, **details):
        self.rows.append((operation, state, details))


class Client:
    gid = 123
    gold = 1000

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def server_now(self):
        return 100

    async def rpc(self, service, method, body=b""):
        expected, reply = self.replies.pop(0)
        assert method == expected
        self.calls.append((method, wire.parse(body)))
        return reply


def test_future_maturity_and_residue():
    land = models.lands(plot(9, [(1, 90), (2, 95), (6, 120)]))[0]
    assert land.status(100) == "growing"
    assert land.status(120) == "mature"
    assert models.lands(plot(9, [(7, 99)]))[0].status(100) == "residue"
    assert replace(land, shared=True).status(100) == "skip"
    assert replace(land, master_id=8).status(100) == "skip"


async def test_dry_run_never_writes():
    client = Client([("AllLands", plot(9, [(6, 99)]) + plot(13))])
    await FarmEngine(Settings("test"), client, Log()).round()
    assert [method for method, _ in client.calls] == ["AllLands"]


async def test_harvest_cleanup_plant_uses_server_replies():
    client = Client(
        [
            ("AllLands", plot(9, [(6, 99)]) + plot(13, unlocked=False)),
            ("Harvest", plot(9, [(7, 100)])),
            ("RemovePlant", plot(9)),
            ("Bag", inventory(1)),
            ("Plant", plot(9, [(1, 100), (6, 148)])),
        ]
    )
    engine = FarmEngine(Settings("test"), client, Log(), apply=True)
    rows = await engine.round()
    assert rows[9].status(100) == "growing"
    assert engine.next_delay(rows) == 50
    harvest = client.calls[1][1]
    assert wire.data(harvest, 1) == b"\x09"
    assert wire.number(harvest, 2) == 123
    assert wire.number(harvest, 3) == 1
    plant = wire.parse(wire.data(client.calls[-1][1], 2))
    assert wire.number(plant, 1) == 20002
    assert wire.data(plant, 2) == b"\x09"


async def test_multiseason_growing_after_harvest_is_not_cleared():
    client = Client(
        [
            ("AllLands", plot(9, [(6, 99)])),
            ("Harvest", plot(9, [(2, 100), (6, 200)])),
        ]
    )
    await FarmEngine(Settings("test"), client, Log(), apply=True).round()
    assert [method for method, _ in client.calls] == ["AllLands", "Harvest"]


async def test_purchase_is_capped_by_budget_and_actual_seed_reply():
    goods = b"".join(
        wire.uint(tag, value)
        for tag, value in [(1, 15), (2, 0), (3, 2), (4, 99), (5, 1), (6, 20002), (7, 1)]
    )
    client = Client(
        [
            ("AllLands", plot(9) + plot(13)),
            ("Bag", inventory(0)),
            ("ShopInfo", wire.blob(1, goods)),
            ("BuyGoods", wire.blob(2, wire.uint(1, 20002) + wire.uint(2, 1))),
            ("Plant", plot(9, [(1, 100), (6, 148)])),
        ]
    )
    settings = replace(Settings("test"), purchase_budget=2)
    await FarmEngine(settings, client, Log(), apply=True).round()
    purchase = client.calls[3][1]
    assert wire.number(purchase, 2) == 1
    assert [method for method, _ in client.calls].count("Plant") == 1


async def test_missing_target_reply_stops_without_cleanup():
    client = Client([
        ("AllLands", plot(9, [(6, 99)])),
        ("Harvest", plot(13)),
        ("AllLands", plot(9, [(6, 99)])),
    ])
    with pytest.raises(UncertainOutcome, match="未能确认"):
        await FarmEngine(Settings("test"), client, Log(), apply=True).round()
    assert [method for method, _ in client.calls] == ["AllLands", "Harvest", "AllLands"]


async def test_24_mature_lands_are_harvested_and_cleared_in_two_requests():
    mature = b"".join(plot(i, [(6, 99)]) for i in range(1, 25))
    residue = b"".join(plot(i, [(7, 100)]) for i in range(1, 25))
    empty = b"".join(plot(i) for i in range(1, 25))
    game = Client(
        [
            ("AllLands", mature),
            ("Harvest", residue),
            ("RemovePlant", empty),
            ("Bag", inventory(0)),
            ("ShopInfo", b""),
        ]
    )
    await FarmEngine(Settings("test"), game, Log(), apply=True).round()
    assert [method for method, _ in game.calls][:3] == ["AllLands", "Harvest", "RemovePlant"]
    assert wire.data(game.calls[1][1], 1) == bytes(range(1, 25))
    assert wire.data(game.calls[2][1], 1) == bytes(range(1, 25))


def test_grouped_schedule_waits_for_last_plot_instead_of_polling_each_one():
    game = Client([])
    rows = {
        land.id: land
        for land in models.lands(plot(1, [(2, 90), (6, 148)]) + plot(24, [(2, 95), (6, 194)]))
    }
    grouped = FarmEngine(Settings("test"), game, Log(), grouped_maturity=True)
    assert grouped.next_delay(rows) == 96
    assert FarmEngine(Settings("test"), game, Log()).next_delay(rows) == 50


async def test_explicit_full_own_farm_mode_includes_shared_empty_lands():
    payload = b"".join(plot(i, shared=i in [7, 8, 9]) for i in range(1, 25))
    game = Client([("AllLands", payload)])
    rows = await FarmEngine(Settings("test"), game, Log(), include_shared=True).get_lands()
    assert len(rows) == 24
    assert rows[7].status(100, allow_shared=True) == "empty"
    game = Client([("AllLands", payload)])
    assert len(await FarmEngine(Settings("test"), game, Log()).get_lands()) == 21


async def test_duplicate_land_ids_stop_before_write():
    client = Client([("AllLands", plot(9) + plot(9))])
    with pytest.raises(FarmError, match="重复 ID"):
        await FarmEngine(Settings("test"), client, Log(), apply=True).round()
