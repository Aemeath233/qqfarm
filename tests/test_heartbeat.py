import asyncio
import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from test_client import Crypto, client

from qqfarm import cli, wire
from qqfarm import client as client_module
from qqfarm.client import PLANT, USER, OperationPaused, UncertainOutcome
from qqfarm.config import FarmError, Settings
from qqfarm.heartbeat import HeartbeatStats, next_deadline
from qqfarm.heartbeat_capture import inspect_capture


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


def reply(method, seq, body):
    meta = wire.text(1, USER) + wire.text(2, method) + wire.uint(3, 2) + wire.uint(4, seq)
    return wire.blob(1, meta) + wire.blob(2, body)


def record(outbound, body, timestamp):
    frame = b"\x82" + bytes([len(body)]) + body
    direction = "Request" if outbound else "Response"
    header = f"{direction}-Length: {len(frame)}\r\nDoneRead: {timestamp}\r\n\r\n"
    return header.encode() + frame + b"\r\n"


def test_capture_report_matches_replies_and_does_not_disclose_credentials(tmp_path):
    path = tmp_path / "test.saz"
    request = wire.uint(1, 123) + wire.text(2, "test-version") + wire.uint(3, 0)
    frames = record(
        False, reply("Login", 1, wire.blob(1, wire.uint(1, 123))), "2026-10-04T21:00:00+08:00"
    )
    for seq, second in [(2, 25), (3, 50)]:
        payload = wire.encode_gate(USER, "Heartbeat", seq, seq - 1, request)
        frames += record(True, payload, f"2026-10-04T21:00:{second}+08:00")
        frames += record(
            False,
            reply("Heartbeat", seq, wire.uint(1, 1791120000000)),
            f"2026-10-04T21:00:{second}.100+08:00",
        )
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "raw/1_c.txt", "GET https://gate-obt.nqf.qq.com/prod/ws?code=private HTTP/1.1\r\n\r\n"
        )
        archive.writestr("raw/1_w.txt", frames)
    result = inspect_capture(path, Crypto())
    sample = result["sessions"][0]
    assert sample["interval_seconds"]["median"] == 25
    assert sample["rtt_seconds"]["median"] == 0.1
    assert sample["request_fields"] == [1, 2, 3]
    assert sample["gid_matches_login"] is True
    assert sample["unanswered_at_export"] == 0
    assert "private" not in json.dumps(result)


async def test_probe_uses_login_and_wait_without_land_queries(tmp_path, monkeypatch):
    class ProbeClient:
        disconnect_reason = "local_stop"
        fault = None

        def __init__(self, settings, session, audit, *, allow_writes):
            assert not allow_writes
            self.heartbeat_stats = HeartbeatStats()

        async def connect(self):
            pass

        async def wait(self, seconds):
            assert seconds == 60
            self.heartbeat_stats.sent_count = self.heartbeat_stats.ack_count = 2

        async def close(self):
            pass

        async def rpc(self, *args):
            pytest.fail("心跳测试不应发送土地、背包或种收请求")

    monkeypatch.setattr(cli, "GameClient", ProbeClient)
    settings = Settings("private", data_dir=tmp_path)
    await cli.run(settings, heartbeat_minutes=1)
    result = json.loads((tmp_path / "heartbeat-last.json").read_text(encoding="utf-8"))
    assert result["completed"] is True
    assert "private" not in str(result)
