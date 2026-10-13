import asyncio
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace

import aiohttp
import pytest

from qqfarm import wire
from qqfarm.client import ITEM, PLANT, GameClient, UncertainOutcome
from qqfarm.config import FarmError, Settings


class Crypto:
    def encrypt(self, data):
        return data

    def decrypt(self, data):
        return data


class Audit:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def record(self, operation, state, **details):
        if self.fail:
            raise sqlite3.OperationalError("local disk failure")
        self.rows.append((operation, state, details))


class Socket:
    closed = False

    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []
        self.disconnect_on_send = False
        self.wrong_service = False
        self.stall_business = False
        self.business_sent = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if frame is None:
            self.closed = True
            raise StopAsyncIteration
        return frame

    async def send_bytes(self, data):
        self.sent.append(data)
        if self.disconnect_on_send:
            await self.frames.put(None)
            return
        request = wire.decode_gate(data)
        if self.stall_business and request["method"] != "Heartbeat":
            self.business_sent.set()
            return
        meta = wire.text(1, ITEM if self.wrong_service else request["service"])
        meta += wire.text(2, request["method"]) + wire.uint(3, 2)
        meta += wire.uint(4, request["seq"]) + wire.uint(5, 77)
        body = wire.uint(1, int(time.time() * 1000)) if request["method"] == "Heartbeat" else b"ok"
        await self.frames.put(
            SimpleNamespace(
                type=aiohttp.WSMsgType.BINARY, data=wire.blob(1, meta) + wire.blob(2, body)
            )
        )

    async def close(self):
        self.closed = True


def client(*, apply=False, audit=None):
    settings = replace(Settings("secret-must-not-be-logged"), request_interval=0)
    result = GameClient(settings, None, audit or Audit(), allow_writes=apply, crypto=Crypto())
    result.ws = Socket()
    return result


async def test_readonly_rejects_writes_before_send():
    game = client()
    with pytest.raises(FarmError, match="只读"):
        await game.rpc(PLANT, "Harvest")
    assert game.ws.sent == []


async def test_receive_correlates_seq_service_method_and_updates_server_seq():
    game = client()
    game.reader = asyncio.create_task(game.receive())
    try:
        assert await game.rpc(PLANT, "AllLands") == b"ok"
        assert game.server_seq == 77
        assert game.pending == {}
        assert "secret-must-not-be-logged" not in str(game.audit.rows)
    finally:
        await game.close()


async def test_wrong_service_stops_session():
    game = client()
    game.ws.wrong_service = True
    game.reader = asyncio.create_task(game.receive())
    try:
        with pytest.raises(FarmError):
            await game.rpc(PLANT, "AllLands")
        assert game.disconnected.is_set()
    finally:
        await game.close()


async def test_unknown_write_outcome_is_not_retried():
    game = client(apply=True)
    game.ws.disconnect_on_send = True
    game.reader = asyncio.create_task(game.receive())
    try:
        with pytest.raises(UncertainOutcome):
            await game.rpc(PLANT, "Harvest", wire.packed(1, [9]))
        assert len(game.ws.sent) == 1
        assert game.pending == {}
        assert any(state == "unknown" for _, state, _ in game.audit.rows)
    finally:
        await game.close()


async def test_audit_failure_leaves_no_pending_request_or_write():
    game = client(apply=True, audit=Audit(fail=True))
    with pytest.raises(sqlite3.OperationalError):
        await game.rpc(PLANT, "Harvest")
    assert game.pending == {}
    assert game.ws.sent == []


async def test_disconnect_wakes_scheduler_immediately():
    game = client()
    game.disconnected.set()
    with pytest.raises(FarmError, match="调度"):
        await asyncio.wait_for(game.wait(300), timeout=0.5)


async def test_pause_interrupts_random_wait_before_any_write(monkeypatch):
    from qqfarm import client as client_module
    from qqfarm.client import OperationPaused

    started = asyncio.Event()
    monkeypatch.setattr(client_module.random, "uniform", lambda *_: started.set() or 3)
    game = client(apply=True)
    game.settings = replace(game.settings, random_action_delays=True)
    task = asyncio.create_task(game.rpc(PLANT, "Harvest"))
    await asyncio.wait_for(started.wait(), 0.5)
    game.allow_writes = False
    with pytest.raises(OperationPaused):
        await asyncio.wait_for(task, 0.5)
    assert not game.ws.sent
    await game.close()


async def test_heartbeat_can_complete_during_random_business_wait(monkeypatch):
    from qqfarm import client as client_module
    from qqfarm.client import USER

    ranges = []
    started = asyncio.Event()

    def sample(low, high):
        ranges.append((low, high))
        started.set()
        return 0.2

    monkeypatch.setattr(client_module.random, "uniform", sample)
    game = client(apply=True)
    game.settings = replace(game.settings, random_action_delays=True)
    game.reader = asyncio.create_task(game.receive())
    task = asyncio.create_task(game.rpc(PLANT, "Harvest"))
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        await game.rpc(USER, "Heartbeat")
        assert wire.decode_gate(game.ws.sent[0])["method"] == "Heartbeat"
        await task
        assert ranges == [(1, 3)]
        assert [wire.decode_gate(value)["method"] for value in game.ws.sent] == [
            "Heartbeat",
            "Harvest",
        ]
    finally:
        await game.close()
