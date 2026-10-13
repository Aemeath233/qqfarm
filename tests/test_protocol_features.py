from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qqfarm import catalog, models, wire
from qqfarm.client import UncertainOutcome
from qqfarm.config import FarmError, Settings
from qqfarm.engine import FarmEngine
from qqfarm.web_policy import Policy
from qqfarm.web_service import FarmService, friend_body


def row(key, *, phase=None, begin=99, mature_at=40000, master=0, linked=(), dry=0, weeds=()):
    value = wire.uint(1, key) + wire.uint(2, 1) + wire.uint(3, 5)
    if phase is not None:
        plant = wire.uint(1, 1021032) + wire.text(2, "琉璃宝荷")
        plant += wire.blob(4, wire.uint(1, phase) + wire.uint(2, begin))
        if phase != 6:
            plant += wire.blob(4, wire.uint(1, 6) + wire.uint(2, mature_at))
        plant += wire.uint(6, dry)
        if weeds:
            plant += wire.packed(12, weeds)
        value += wire.blob(10, plant)
    if master:
        value += wire.uint(13, master)
    if linked:
        value += wire.packed(14, linked) + wire.uint(15, 2)
    return value


def payload(*values):
    return b"".join(wire.blob(1, value) for value in values)


def large_crop(phase):
    return payload(
        row(5, phase=phase, linked=[1, 6, 2]), row(6, master=5), row(1, master=5), row(2, master=5)
    )


def bag(seed, count):
    return wire.blob(1, wire.blob(1, wire.uint(1, seed) + wire.uint(2, count)))


class Client:
    gid, gold = 123, 1000

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def server_now(self):
        return 100

    async def rpc(self, service, method, body=b""):
        expected, response = self.replies.pop(0)
        assert expected == method
        self.calls.append((method, wire.parse(body)))
        return response


class Log:
    def __init__(self):
        self.rows = []

    def record(self, *args, **kwargs):
        self.rows.append((args, kwargs))


async def test_large_crop_uses_one_seed_for_four_distinct_cells():
    client = Client(
        [
            ("AllLands", payload(*(row(key) for key in range(1, 25)))),
            ("Bag", bag(21032, 1)),
            ("Plant", large_crop(1)),
        ]
    )
    log = Log()
    rows = await FarmEngine(Settings("test", seed_id=21032), client, log, apply=True).round()
    assert [method for method, _ in client.calls] == ["AllLands", "Bag", "Plant"]
    item = wire.parse(wire.data(client.calls[-1][1], 2))
    assert wire.number(item, 1) == 21032
    assert list(models.repeated_numbers(item[2])) == [5, 6, 1, 2]
    assert rows[5].occupied_ids == (5, 1, 6, 2)
    assert all(rows[key].master_id == 5 for key in [1, 2, 6])
    assert len([entry for entry in log.rows if entry[0][0] == "plant"]) == 1


async def test_large_crop_harvest_and_cleanup_only_target_master_then_release_children():
    client = Client(
        [
            ("AllLands", large_crop(6)),
            ("Harvest", large_crop(7)),
            ("RemovePlant", payload(*(row(key) for key in [5, 6, 1, 2]))),
            ("Bag", bag(20002, 0)),
            ("ShopInfo", b""),
        ]
    )
    rows = await FarmEngine(Settings("test"), client, Log(), apply=True).round()
    assert list(models.repeated_numbers(client.calls[1][1][1])) == [5]
    assert list(models.repeated_numbers(client.calls[2][1][1])) == [5]
    assert all(value.status(100) == "empty" for value in rows.values())
    assert all(not value.master_id for value in rows.values())


async def test_large_crop_respects_physical_land_limit():
    client = Client([("AllLands", large_crop(6))])
    assert await FarmEngine(Settings("test", max_lands=3), client, Log()).get_lands() == {}


async def test_inconsistent_large_crop_cannot_be_operated():
    malformed = payload(
        row(5, phase=6, linked=[1, 6, 2]), row(1), row(6, master=5), row(2, master=5)
    )
    client = Client([("AllLands", malformed)])
    with pytest.raises(FarmError, match="不一致"):
        await FarmEngine(Settings("test"), client, Log(), apply=True).round()
    assert len(client.calls) == 1


async def test_maturity_grace_does_not_harvest_an_already_mature_crop_early():
    client = Client([("AllLands", payload(row(1, phase=6)))])
    settings = Settings("test", maturity_grace_seconds=300)
    engine = FarmEngine(settings, client, Log(), apply=True)
    rows = await engine.round()
    assert [method for method, _ in client.calls] == ["AllLands"]
    assert engine.next_delay(rows) == 301


def test_long_crop_waits_until_actual_maturity_plus_grace_without_five_minute_polling():
    client = Client([])
    engine = FarmEngine(Settings("test", maturity_grace_seconds=300), client, Log())
    plant = models.land(row(1, phase=1, begin=100, mature_at=18100))
    assert engine.next_delay({1: plant}) == 18302


async def test_random_plant_order_never_reuses_a_cell(monkeypatch):
    from qqfarm import engine as engine_module

    monkeypatch.setattr(engine_module.random, "shuffle", lambda groups: groups.reverse())
    client = Client(
        [
            ("AllLands", payload(row(1), row(2), row(3))),
            ("Bag", bag(20002, 3)),
            ("Plant", payload(row(3, phase=1))),
            ("Plant", payload(row(2, phase=1))),
            ("Plant", payload(row(1, phase=1))),
        ]
    )
    await FarmEngine(Settings("test", random_plant_order=True), client, Log(), apply=True).round()
    planted = [
        list(models.repeated_numbers(wire.parse(wire.data(f, 2))[2]))[0]
        for method, f in client.calls
        if method == "Plant"
    ]
    assert planted == [3, 2, 1] and len(set(planted)) == 3


def task(key, *, progress=1, total=1, claimed=False, unlocked=True, kind=2):
    return b"".join(
        wire.uint(tag, value)
        for tag, value in (
            (1, key),
            (2, progress),
            (3, int(claimed)),
            (4, int(unlocked)),
            (6, total),
            (10, kind),
        )
    )


async def test_daily_task_detection_claims_only_eligible_tasks_in_one_batch(tmp_path):
    service = FarmService(tmp_path)
    before = b"".join(
        wire.blob(3, value)
        for value in (
            task(1),
            task(2),
            task(3, progress=0),
            task(4, claimed=True),
            task(5, unlocked=False),
            task(6, kind=1),
        )
    )
    after = wire.blob(3, task(1, claimed=True)) + wire.blob(3, task(2, claimed=True))

    async def rpc(_service, method, body=b""):
        if method == "TaskInfo":
            return wire.blob(1, before)
        assert method == "BatchClaimTaskReward"
        assert list(models.repeated_numbers(wire.parse(body)[1])) == [1, 2]
        return wire.blob(2, after)

    service.client = SimpleNamespace(closed=False, fault=None, rpc=AsyncMock(side_effect=rpc))
    try:
        await service.claim_locked(None)
        assert service.client.rpc.await_count == 2 and service.metrics["claimed"] == 2
    finally:
        service.audit.close()


async def test_partial_batch_task_confirmation_stops_without_retry(tmp_path):
    service = FarmService(tmp_path)
    before = wire.blob(1, wire.blob(3, task(1)) + wire.blob(3, task(2)))
    service.client = SimpleNamespace(
        closed=False,
        fault=None,
        rpc=AsyncMock(side_effect=[before, wire.blob(2, wire.blob(3, task(1, claimed=True)))]),
    )
    try:
        with pytest.raises(UncertainOutcome):
            await service.claim_locked(None)
        assert service.client.rpc.await_count == 2
        assert service.metrics["claimed"] == 0
    finally:
        service.audit.close()


async def test_friend_help_uses_farming_with_last_duplicate_update(tmp_path):
    service = FarmService(tmp_path)
    service.friends = [{"id": 456}]

    async def rpc(_service, method, body):
        f = wire.parse(body)
        if method == "Enter":
            return wire.blob(1, wire.uint(1, 456)) + wire.blob(
                2, row(1, phase=2, dry=1, weeds=[777])
            )
        if method == "Farming":
            assert wire.number(f, 2) == 456 and wire.number(f, 4) == 2
            assert list(models.repeated_numbers(f[1])) == [1]
            return payload(row(1, phase=2, weeds=[777]), row(1, phase=2))
        assert method == "Leave"
        return b""

    service.client = SimpleNamespace(
        gid=123, closed=False, fault=None, server_now=lambda: 100, rpc=AsyncMock(side_effect=rpc)
    )
    try:
        await service.friend_locked("help", 456)
        assert [call.args[1] for call in service.client.rpc.call_args_list] == [
            "Enter",
            "Farming",
            "Leave",
        ]
    finally:
        service.audit.close()


def test_friend_harvest_uses_the_captured_visit_source():
    fields = wire.parse(friend_body("Harvest", 456, [1, 2]))
    assert wire.number(fields, 5) == 2


def test_observed_large_seeds_are_inventory_only_and_sell_guards_remain():
    rows = catalog.seeds(b"", {21032: 1, 29004: 1})
    rows = [value for value in rows if value["seed_id"] in {21032, 29004}]
    assert len(rows) == 2
    assert all(
        value["inventory_only"] and value["plot_size"] == 2 and value["price"] == 0
        for value in rows
    )
    inventory = catalog.inventory(
        wire.blob(
            1, payload(wire.uint(1, 41032) + wire.uint(2, 1), wire.uint(1, 49004) + wire.uint(2, 1))
        )
    )
    assert [item["sellable"] for item in inventory] == [False, True]


def test_policy_and_settings_reject_reversed_random_delay_range():
    with pytest.raises(ValueError):
        Policy(action_delay_min=3, action_delay_max=1)
    with pytest.raises(FarmError):
        replace(Settings("test"), action_delay_min=3, action_delay_max=1)
