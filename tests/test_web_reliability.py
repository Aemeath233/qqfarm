import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from qqfarm import models, wire
from qqfarm.audit import Audit, InstanceLock
from qqfarm.client import OperationPaused, RPCError
from qqfarm.config import FarmError, Settings
from qqfarm.engine import FarmEngine
from qqfarm.friend_stats import FriendStats
from qqfarm.web_api import create_app
from qqfarm.web_policy import Policy
from qqfarm.web_service import FarmService

HEADERS = {"X-Farm-Action": "personal-control"}
PASSWORD = "local-review-test-password"


def plot(identifier, mature_at=None):
    row = wire.uint(1, identifier) + wire.uint(2, 1)
    if mature_at:
        plant = wire.uint(1, 1020002) + wire.text(2, "白萝卜")
        plant += wire.blob(4, wire.uint(1, 1) + wire.uint(2, 90))
        plant += wire.blob(4, wire.uint(1, 6) + wire.uint(2, mature_at))
        row += wire.blob(10, plant)
    return wire.blob(1, row)


def test_old_mail_setting_is_retired_without_losing_the_saved_plan(tmp_path):
    stored = Policy(seed_priority=[20003], maturity_grace_seconds=5).model_dump()
    stored["auto_mail"] = True
    (tmp_path / "policy.json").write_text(json.dumps(stored))
    service = FarmService(tmp_path)
    try:
        assert service.policy.seed_priority == [20003]
        assert service.policy.maturity_grace_seconds == 5
        assert "auto_mail" not in service.policy.model_dump()
        assert Policy().maturity_grace_seconds == 60
    finally:
        service.audit.close()


def test_mail_api_is_removed_and_empty_priority_can_be_saved(tmp_path):
    app = create_app(tmp_path, password=PASSWORD)
    with TestClient(app, base_url="http://localhost") as client:
        assert (
            client.post("/api/login", headers=HEADERS, json={"password": PASSWORD}).status_code
            == 200
        )
        assert client.post("/api/mail/claim", headers=HEADERS, json={}).status_code == 404
        response = client.patch(
            "/api/policy", headers=HEADERS, json={"seed_priority": [], "fallback_radish": False}
        )
        assert response.status_code == 200
        assert response.json()["seed_priority"] == []
        status = client.get("/api/status").json()
        assert "emails" not in status
        assert "empty_seed_priority" in status["capabilities"]


@pytest.mark.parametrize("length", ["not-a-number", "-1"])
def test_invalid_content_length_is_a_client_error(tmp_path, length):
    app = create_app(tmp_path, password=PASSWORD)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/api/login", headers={**HEADERS, "Content-Length": length}, content=b"{}"
        )
        assert response.status_code == 400


def test_chunked_request_cannot_bypass_the_body_limit(tmp_path):
    app = create_app(tmp_path, password=PASSWORD)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/api/login",
            headers={**HEADERS, "Content-Type": "application/json"},
            content=iter([b'{"password":"', b"x" * 200000, b'"}']),
        )
        assert response.status_code == 413
        assert "xxxxx" not in response.text


async def test_no_priority_and_no_fallback_only_harvests_existing_crops():
    game = SimpleNamespace(gid=123, gold=100, server_now=lambda: 100)
    game.rpc = AsyncMock(side_effect=[plot(1, 99) + plot(2), plot(1)])
    audit = SimpleNamespace(record=lambda *args, **kwargs: None)
    engine = FarmEngine(
        Settings("test"), game, audit, apply=True, seed_priority=[], fallback_radish=False
    )
    rows = await engine.round()
    assert [call.args[1] for call in game.rpc.call_args_list] == ["AllLands", "Harvest"]
    assert not rows[1].plant_id and not rows[2].plant_id


async def test_confirmed_plant_is_visible_even_when_the_next_plant_is_rejected(tmp_path):
    bag = wire.blob(1, wire.blob(1, wire.uint(1, 20002) + wire.uint(2, 2)))
    game = SimpleNamespace(gid=123, gold=100, server_now=lambda: 100)
    game.rpc = AsyncMock(side_effect=[plot(1) + plot(2), bag, plot(1, 200), RPCError("Plant", 7)])
    service = FarmService(tmp_path)
    service.client = game
    engine = FarmEngine(
        Settings("test"),
        game,
        service.audit,
        apply=True,
        seed_priority=[20002],
        purchase_mode="inventory",
    )
    try:
        with pytest.raises(RPCError):
            await engine.round()
        service.sync_engine_state(engine)
        assert service.rows[1].plant_id == 1020002
        assert service.rows[2].plant_id == 0
        assert engine.inventory[20002] == 1
    finally:
        service.audit.close()


def test_next_check_uses_maturity_after_fertilizer(tmp_path, monkeypatch):
    service = FarmService(tmp_path)
    before = models.lands(plot(1, 1000))[0]
    after = replace(before, phases=(models.Phase(1, 0, 90), models.Phase(6, 0, 200)))
    service.rows = {1: after}
    game = SimpleNamespace(server_now=lambda: 100)
    engine = FarmEngine(Settings("test", maturity_grace_seconds=60), game, service.audit)
    monkeypatch.setattr("qqfarm.web_service.time.time", lambda: 10000)
    try:
        assert service.next_own_check(engine, {1: before}) == 10162
    finally:
        service.audit.close()


@pytest.mark.parametrize("exception", [FarmError("constructor failed"), asyncio.CancelledError()])
async def test_login_constructor_failure_preserves_existing_farm_state(
    tmp_path, monkeypatch, exception
):
    service = FarmService(tmp_path)
    service.rows = {1: models.lands(plot(1, 200))[0]}
    service.metrics["planted"] = 5
    monkeypatch.setattr(
        "qqfarm.web_service.GameClient", lambda *args, **kwargs: (_ for _ in ()).throw(exception)
    )
    try:
        with pytest.raises(type(exception)):
            await service.login({"code": "test", "version": "test"})
        assert service.session is None and service.client is None and service.lock is None
        assert 1 in service.rows and service.metrics["planted"] == 5
        with InstanceLock(tmp_path / "account.lock"):
            pass
    finally:
        service.audit.close()


def test_old_log_pruning_does_not_reset_ids_below_the_statistics_cursor(tmp_path):
    path = tmp_path / "audit.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE operations (id INTEGER PRIMARY KEY, at REAL, instance TEXT, "
            "account TEXT, operation TEXT, state TEXT, details TEXT)"
        )
        db.execute(
            "INSERT INTO operations VALUES(500,?,?,?,'heartbeat','success','{}')",
            (time.time() - 8 * 86400, "old", "account"),
        )
        db.execute("CREATE TABLE friend_stats_cursor(id INTEGER PRIMARY KEY,cursor INTEGER)")
        db.execute("INSERT INTO friend_stats_cursor VALUES(1,500)")
    audit = Audit(path)
    stats = FriendStats(audit.db)
    audit.account = "account"
    try:
        audit.record("friend_help", "confirmed", friend="friend", plots=[1, 2])
        stats.update()
        assert audit.db.execute("SELECT id FROM operations").fetchone()[0] == 501
        assert stats.snapshot("account", time.time())["totals"]["help"]["plots"] == 2
    finally:
        audit.close()


def test_log_retention_runs_without_a_service_restart(tmp_path, monkeypatch):
    now = time.time()
    audit = Audit(tmp_path / "audit.sqlite3")
    try:
        audit.record("plant", "confirmed", land=1)
        monkeypatch.setattr("qqfarm.audit.time.time", lambda: now + 8 * 86400)
        audit.record("heartbeat", "success")
        assert audit.db.execute("SELECT operation FROM operations").fetchall() == [("heartbeat",)]
        assert audit.db.execute("SELECT id FROM operations").fetchone()[0] == 2
    finally:
        audit.close()


async def test_remote_disconnect_releases_account_lock_and_marks_snapshot_stale(tmp_path):
    service = FarmService(tmp_path)
    client = SimpleNamespace(
        closed=False,
        fault=FarmError("游戏要求下线"),
        disconnect_reason="server_kickout",
        disconnected=asyncio.Event(),
        allow_writes=False,
        close=AsyncMock(),
    )
    service.client = client
    service.session = SimpleNamespace(close=AsyncMock())
    service.lock = InstanceLock(tmp_path / "account.lock")
    service.lock.__enter__()
    service.mode = "connected"
    service.rows = {1: models.lands(plot(1, int(time.time()) - 1))[0]}
    service.updated_at = time.time() - 60
    observer = asyncio.create_task(service.observe())
    service.observer = observer
    try:
        await asyncio.sleep(0)
        client.disconnected.set()
        await asyncio.wait_for(observer, timeout=2)
        status = service.view()
        assert status["mode"] == "offline"
        assert not status["snapshot_fresh"]
        assert status["lands"][0]["status"] == "mature"
        assert "具体原因无法" in status["note"]
        assert service.client is None and service.session is None and service.lock is None
        with InstanceLock(tmp_path / "account.lock"):
            pass
    finally:
        if not observer.done():
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        await service.close()


async def test_refresh_is_not_skipped_after_partial_state_update(tmp_path):
    service = FarmService(tmp_path)
    service.full_refresh_at = time.time() - 30
    service.updated_at = time.time()
    service.refresh_locked = AsyncMock()
    try:
        assert await service.refresh() is True
        service.refresh_locked.assert_awaited_once()
        service.full_refresh_at = time.time()
        assert await service.refresh() is False
        service.refresh_locked.assert_awaited_once()
    finally:
        service.audit.close()


async def test_pause_cancels_queued_manual_work_before_it_can_write(tmp_path):
    service = FarmService(tmp_path)
    service.client = SimpleNamespace(closed=False, fault=None, allow_writes=False)
    await service.guard.acquire()
    task = asyncio.create_task(service.manual("claim", operation_id="queued-test"))
    await asyncio.sleep(0)
    try:
        assert service.manual_waiters == 1
        await service.pause()
        service.guard.release()
        with pytest.raises(OperationPaused, match="取消排队"):
            await task
        assert service.manual_waiters == 0 and not service.operations
    finally:
        if service.guard.locked():
            service.guard.release()
        service.audit.close()
