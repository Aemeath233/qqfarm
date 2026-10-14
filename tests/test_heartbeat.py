import asyncio
from types import SimpleNamespace

import pytest
from test_client import client

from qqfarm import client as client_module
from qqfarm import wire
from qqfarm.client import PLANT, USER, OperationPaused, UncertainOutcome
from qqfarm.config import FarmError
from qqfarm.heartbeat import next_deadline


def test_deadlines_do_not_accumulate_reply_delay_or_burst_after_pause():
    assert next_deadline(25, 27) == 50
    assert next_deadline(50, 52) == 75
    assert next_deadline(75, 140) == 165


async def test_business_reply_wait_does_not_block_heartbeat():
    game = client()
    game.ws.stall_business = True
    game.reader = asyncio.create_task(game.receive())
    business = asyncio.create_task(game.rpc(PLANT, "AllLands"))
    try:
        await asyncio.wait_for(game.ws.business_sent.wait(), timeout=1)
        payload = wire.uint(1, 123) + wire.text(2, game.settings.client_version) + wire.uint(3, 0)
        await asyncio.wait_for(game.rpc(USER, "Heartbeat", payload), timeout=1)
        assert not business.done()
        assert game.heartbeat_stats.ack_count == 1
        assert len(game.pending) == 1
        assert [wire.decode_gate(row)["method"] for row in game.ws.sent] == [
            "AllLands",
            "Heartbeat",
        ]
    finally:
        business.cancel()
        with pytest.raises(asyncio.CancelledError):
            await business
        await game.close()


async def test_keepalive_uses_capture_fields_and_fixed_cadence(monkeypatch):
    game = client()
    game.gid = 123
    clock = {"now": 0}
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    due_times = []

    async def wait(seconds):
        clock["now"] += seconds

    async def rpc(service, method, payload, *, due_at):
        assert (service, method) == (USER, "Heartbeat")
        fields = wire.parse(payload)
        assert wire.number(fields, 1) == 123
        assert wire.string(fields, 2) == game.settings.client_version
        assert fields[3] == [0]
        due_times.append(due_at)
        clock["now"] += 2  # 回应慢两秒仍不应让下一个发送周期变成 27 秒。
        if len(due_times) == 3:
            game.closed = True

    game.wait, game.rpc = wait, rpc
    await game.keepalive()
    assert due_times == [25, 50, 75]


async def test_send_backpressure_is_bounded_and_unknown_write_not_retried(monkeypatch):
    game = client(apply=True)
    monkeypatch.setattr(client_module, "ACK_TIMEOUT", 0.03)
    attempted = []

    async def blocked(data):
        attempted.append(data)
        await asyncio.Event().wait()

    game.ws.send_bytes = blocked
    with pytest.raises(UncertainOutcome):
        await game.rpc(PLANT, "Harvest")
    assert len(attempted) == 1
    assert game.pending == {}


async def test_pausing_during_send_queue_prevents_next_write():
    game = client(apply=True)
    await game.send_lock.acquire()
    request = asyncio.create_task(game.rpc(PLANT, "Plant"))
    await asyncio.sleep(0)
    game.allow_writes = False
    game.send_lock.release()
    with pytest.raises(OperationPaused):
        await request
    assert game.ws.sent == []
    assert game.pending == {}


async def test_failed_heartbeat_closes_session_and_wakes_scheduler():
    game = client()

    async def failure(*args, **kwargs):
        raise FarmError("no response")

    game.rpc = failure
    await game.keepalive(interval=0.001)
    assert game.ws.closed
    assert game.disconnected.is_set()
    assert game.disconnect_reason == "heartbeat_failed"
    assert game.heartbeat_stats.ack_count == 0


async def test_requested_stop_does_not_report_network_failure_or_close_pending_business():
    game = client()
    game.stop_requested = True
    game.disconnected.set()
    await game.keepalive(interval=0.001)
    assert game.disconnect_reason == "local_stop"
    assert not game.ws.closed
    assert not any(state == "failed" for _, state, _ in game.audit.rows)
