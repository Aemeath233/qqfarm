import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from qqfarm import wire
from qqfarm.client import RPCError, UncertainOutcome
from qqfarm.config import FarmError, Settings
from qqfarm.friend_stats import day_key
from qqfarm.web_api import create_app
from qqfarm.web_policy import Policy
from qqfarm.web_service import FarmService, FriendInteractionPaused, friend_body

HEADERS = {"X-Farm-Action": "personal-control"}
PASSWORD = "local-panel-test-password"


@pytest.fixture
def panel(tmp_path):
    app = create_app(tmp_path, password=PASSWORD)
    with TestClient(app, base_url="http://localhost") as client:
        yield app.state.service, client


def authenticate(client):
    response = client.post("/api/login", headers=HEADERS, json={"password": PASSWORD})
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()


def test_panel_requires_password_and_rejects_cross_origin_writes(panel):
    _, client = panel
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 403
    assert (
        client.post(
            "/api/login",
            headers={**HEADERS, "Origin": "https://external.test"},
            json={"password": PASSWORD},
        ).status_code
        == 403
    )
    authenticate(client)
    assert client.get("/api/status").status_code == 200


def test_public_status_and_validation_never_echo_authorization(panel):
    service, client = panel
    authenticate(client)
    service.profile = {"code": "very-private-farm-code", "friend_open_ids": ["private-friend-key"]}
    assert "private" not in client.get("/api/status").text
    result = client.post(
        "/api/authorization/saz",
        headers=HEADERS,
        json={"code": "very-private-farm-code", "version": 7},
    )
    assert result.status_code == 422
    assert "very-private" not in result.text


def test_page_status_reads_cache_without_game_requests(panel):
    service, client = panel
    authenticate(client)
    service.client = SimpleNamespace(
        gold=3,
        closed=True,
        heartbeat_stats=SimpleNamespace(snapshot=lambda: {}),
        rpc=AsyncMock(),
        close=AsyncMock(),
    )
    for _ in range(5):
        assert client.get("/api/status").status_code == 200
    service.client.rpc.assert_not_awaited()


def test_policy_persists_separate_priorities_and_rejects_unknown_friends(panel):
    service, client = panel
    authenticate(client)
    service.seeds = [{"seed_id": 20002}, {"seed_id": 20003}]
    service.friends = [{"id": 101}, {"id": 102}]
    policy = Policy(seed_priority=[20003, 20002], steal_priority=[101], weeds_priority=[102])
    result = client.put("/api/policy", headers=HEADERS, json=policy.model_dump())
    assert result.status_code == 200
    saved = json.loads((service.root / "policy.json").read_text())
    assert saved["steal_priority"] == [101] and saved["weeds_priority"] == [102]
    result = client.put(
        "/api/policy", headers=HEADERS, json={**policy.model_dump(), "insects_priority": [999]}
    )
    assert result.status_code == 409


def test_duplicate_mutation_is_never_sent_twice(panel):
    service, client = panel
    authenticate(client)
    service.manual = AsyncMock()
    headers = {**HEADERS, "Idempotency-Key": "one-unique-click"}
    value = {"task_id": 200001}
    assert client.post("/api/tasks/claim", headers=headers, json=value).status_code == 200
    assert client.post("/api/tasks/claim", headers=headers, json=value).status_code == 409
    service.manual.assert_awaited_once()


@pytest.mark.parametrize("method", ["PutWeeds", "PutInsects"])
def test_current_friend_put_request_has_host_first_and_packed_plots_second(method):
    fields = wire.parse(friend_body(method, 12345, [15]))
    assert wire.number(fields, 1) == 12345
    assert wire.data(fields, 2) == b"\x0f"
    assert wire.number(fields, 3) == 0 and wire.number(fields, 4) == 2


def task(task_id=1, claimed=False, progress=1, total=1, kind=2):
    return (
        wire.uint(1, task_id)
        + wire.uint(2, progress)
        + wire.uint(3, int(claimed))
        + wire.uint(4, 1)
        + wire.uint(6, total)
        + wire.text(9, "每日登录游戏")
        + wire.uint(10, kind)
    )


async def test_auto_task_claims_only_completed_unclaimed_daily_tasks(tmp_path):
    service = FarmService(tmp_path)
    info = (
        wire.blob(3, task(1))
        + wire.blob(3, task(2, claimed=True))
        + wire.blob(3, task(3, progress=0))
        + wire.blob(3, task(4, kind=1))
    )

    async def rpc(_service, method, body=b""):
        if method == "TaskInfo":
            return wire.blob(1, info)
        assert method == "ClaimTaskReward" and wire.number(wire.parse(body), 1) == 1
        assert wire.number(wire.parse(body), 2) == 0
        return wire.blob(2, wire.blob(3, task(1, claimed=True)))

    service.client = SimpleNamespace(closed=False, fault=None, rpc=AsyncMock(side_effect=rpc))
    try:
        await service.claim_locked(None)
        assert service.client.rpc.await_count == 2
        assert service.metrics["claimed"] == 1
    finally:
        service.audit.close()


async def test_unconfirmed_claim_does_not_count_as_success(tmp_path):
    service = FarmService(tmp_path)
    service.client = SimpleNamespace(
        closed=False,
        fault=None,
        rpc=AsyncMock(
            side_effect=[
                wire.blob(1, wire.blob(3, task())),
                b"",
                wire.blob(1, wire.blob(3, task())),
            ]
        ),
    )
    try:
        with pytest.raises(UncertainOutcome):
            await service.claim_locked(1)
        assert service.metrics["claimed"] == 0
        assert [call.args[1] for call in service.client.rpc.await_args_list] == [
            "TaskInfo", "ClaimTaskReward", "TaskInfo"
        ]
    finally:
        service.audit.close()


async def test_claim_missing_reply_state_is_confirmed_by_task_query(tmp_path):
    service = FarmService(tmp_path)
    service.client = SimpleNamespace(
        closed=False,
        fault=None,
        rpc=AsyncMock(
            side_effect=[
                wire.blob(1, wire.blob(3, task())),
                b"",
                wire.blob(1, wire.blob(3, task(claimed=True))),
            ]
        ),
    )
    try:
        await service.claim_locked(1)
        assert service.metrics["claimed"] == 1
        assert service.tasks[0]["claimed"]
        assert [call.args[1] for call in service.client.rpc.await_args_list] == [
            "TaskInfo", "ClaimTaskReward", "TaskInfo"
        ]
    finally:
        service.audit.close()


def bag(rows):
    return wire.blob(
        1, b"".join(wire.blob(1, wire.uint(1, id) + wire.uint(2, count)) for id, count in rows)
    )


async def test_sale_rejects_seed_and_overselling_before_write(tmp_path):
    service = FarmService(tmp_path)
    service.client = SimpleNamespace(
        closed=False, fault=None, rpc=AsyncMock(return_value=bag([(20002, 24), (40002, 20)]))
    )
    try:
        for items in [[{"id": 20002, "count": 1}], [{"id": 40002, "count": 21}]]:
            with pytest.raises(FarmError):
                await service.sell_locked(items)
        assert all(call.args[1] == "Bag" for call in service.client.rpc.await_args_list)
    finally:
        service.audit.close()


async def test_sale_preserves_stack_uid_in_captured_field_six(tmp_path):
    service = FarmService(tmp_path)
    item = wire.uint(1, 40002) + wire.uint(2, 20) + wire.uint(6, 2864)

    async def rpc(_service, method, body=b""):
        if method == "Bag":
            return wire.blob(1, wire.blob(1, item))
        fields = wire.parse(wire.parse(body)[1][0])
        assert wire.number(fields, 6) == 2864
        assert wire.number(fields, 1) == 40002 and wire.number(fields, 2) == 3
        assert 3 not in fields
        return wire.blob(1, wire.uint(1, 40002) + wire.uint(2, 3) + wire.uint(6, 2864))

    service.client = SimpleNamespace(closed=False, fault=None, rpc=AsyncMock(side_effect=rpc))
    try:
        await service.sell_locked([{"id": 40002, "uid": 2864, "count": 3}])
        assert service.metrics["sold"] == 3
    finally:
        service.audit.close()


def land_payload(*, insects=False, mature_at=2000, stealable=False, left_fruit=0):
    plant = wire.uint(1, 1020002) + wire.text(2, "白萝卜")
    plant += wire.blob(4, wire.uint(1, 1) + wire.uint(2, 1)) + wire.blob(
        4, wire.uint(1, 6) + wire.uint(2, mature_at)
    )
    if insects:
        plant += wire.packed(13, [7])
    if stealable:
        plant += wire.uint(16, 1) + wire.uint(18, left_fruit)
    return wire.uint(1, 15) + wire.uint(2, 1) + wire.blob(10, plant)


async def test_friend_limit_prevents_put_and_always_leaves_farm(tmp_path):
    service = FarmService(tmp_path)
    service.friends = [{"id": 12345}]
    service.limits[10004] = {"used": 100, "maximum": 100}
    service.limits_day = day_key(1000)

    async def rpc(_service, method, body=b""):
        if method == "Enter":
            return wire.blob(1, wire.uint(1, 12345)) + wire.blob(2, land_payload())
        assert method == "Leave"
        return b""

    service.client = SimpleNamespace(
        gid=7, closed=False, fault=None, server_now=lambda: 1000, rpc=AsyncMock(side_effect=rpc)
    )
    try:
        await service.friend_locked("insects", 12345)
        assert service.client.rpc.await_count == 0
    finally:
        service.audit.close()


async def test_friend_identity_mismatch_is_not_followed_by_any_write(tmp_path):
    service = FarmService(tmp_path)
    service.friends = [{"id": 12345}]
    service.client = SimpleNamespace(
        gid=7,
        closed=False,
        fault=None,
        server_now=lambda: 1000,
        rpc=AsyncMock(side_effect=[wire.blob(1, wire.uint(1, 999)), b""]),
    )
    try:
        with pytest.raises(FarmError):
            await service.friend_locked("weeds", 12345)
        assert [call.args[1] for call in service.client.rpc.await_args_list] == ["Enter", "Leave"]
    finally:
        service.audit.close()


async def test_friend_insect_reply_without_lands_uses_read_only_verification(tmp_path):
    service = FarmService(tmp_path)
    service.friends = [{"id": 12345}]
    entries = 0

    async def rpc(_service, method, body=b""):
        nonlocal entries
        if method == "Enter":
            entries += 1
            return wire.blob(1, wire.uint(1, 12345)) + wire.blob(
                2, land_payload(insects=entries > 1)
            )
        assert method in {"PutInsects", "Leave"}
        return b""

    service.client = SimpleNamespace(
        gid=7, closed=False, fault=None, server_now=lambda: 1000,
        rpc=AsyncMock(side_effect=rpc),
    )
    try:
        await service.friend_locked("insects", 12345)
        methods = [call.args[1] for call in service.client.rpc.await_args_list]
        assert methods == ["Enter", "PutInsects", "Leave", "Enter", "Leave"]
        assert service.metrics["friend_actions"] == 1
    finally:
        service.audit.close()


async def test_friend_insect_unconfirmed_after_revisit_pauses_only_that_kind(tmp_path):
    service = FarmService(tmp_path)
    service.friends = [{"id": 12345}]

    async def rpc(_service, method, body=b""):
        if method == "Enter":
            return wire.blob(1, wire.uint(1, 12345)) + wire.blob(2, land_payload())
        assert method in {"PutInsects", "Leave"}
        return b""

    service.client = SimpleNamespace(
        gid=7, closed=False, fault=None, server_now=lambda: 1000,
        rpc=AsyncMock(side_effect=rpc),
    )
    try:
        with pytest.raises(FriendInteractionPaused, match="已暂停放虫"):
            await service.friend_locked("insects", 12345)
        assert [call.args[1] for call in service.client.rpc.await_args_list].count(
            "PutInsects"
        ) == 1
        assert service.metrics["friend_actions"] == 0
        assert "insects" in service.friend_stats.blocks(service.audit.account, 1000)
    finally:
        service.audit.close()


def test_seed_choice_uses_priority_unlock_price_and_inventory(tmp_path):
    service = FarmService(tmp_path)
    service.client = SimpleNamespace(gold=500)
    service.seeds = [
        {"seed_id": 20003, "unlocked": False, "inventory": 0, "price": 4},
        {"seed_id": 20002, "unlocked": True, "inventory": 0, "price": 2},
    ]
    try:
        assert service.choose_seed(Policy(seed_priority=[20003, 20002])) == 20002
    finally:
        service.audit.close()


async def test_friend_cycle_never_targets_own_account(tmp_path):
    service = FarmService(tmp_path)
    service.running = True
    service.client = SimpleNamespace(
        gid=7,
        closed=False,
        fault=None,
        server_now=lambda: 1000,
        settings=Settings("test"),
    )
    service.friends = [{"id": 7}, {"id": 12345}]
    service.friend_summary_at = time.time()
    service.friend_locked = AsyncMock()
    try:
        await service.friend_cycle(Policy(friend_insects=True, friends_scope="all"))
        service.friend_locked.assert_awaited_once_with("insects", 12345)
    finally:
        service.audit.close()


async def test_paused_friend_kind_does_not_stop_own_farm_schedule(tmp_path):
    service = FarmService(tmp_path)
    service.running = True
    service.client = SimpleNamespace(
        gid=7,
        closed=False,
        fault=None,
        server_now=lambda: 1000,
        settings=Settings("test"),
        allow_writes=False,
    )
    service.friends = [{"id": 12345}]
    service.friend_summary_at = time.time()
    service.friend_locked = AsyncMock(side_effect=FriendInteractionPaused("状态未确认"))
    try:
        await service.friend_cycle(Policy(friend_insects=True, insects_priority=[12345]))
        assert service.running
        assert not service.client.allow_writes
        service.friend_locked.assert_awaited_once_with("insects", 12345)
    finally:
        service.audit.close()


def test_friend_batches_rotate_and_include_due_crop(tmp_path):
    service = FarmService(tmp_path)
    try:
        priority = [11, 22, 33, 44, 55]
        assert service.friend_batch("steal", priority, 2) == [11, 22]
        service.friend_maturities[55] = time.time() + 3
        assert service.friend_batch("steal", priority, 2) == [55, 33]
        assert service.friend_batch("help", priority, 2) == [11, 22]
        assert service.friend_batch("help", priority, 2) == [33, 44]
        service.friend_cursor["steal"] = 0
        service.friend_maturities = {22: time.time() + 3}
        assert service.friend_batch("steal", priority, 2) == [22, 11]
        service.friend_maturities.clear()
        service.friend_cursor["steal"] = 0
        service.friends = [{"id": 55, "stealable_plots": 4}]
        assert service.friend_batch("steal", priority, 2) == [11, 55]
    finally:
        service.audit.close()


async def test_friend_summary_refresh_preserves_names_and_prioritizes_hints(tmp_path):
    service = FarmService(tmp_path)
    service.friends = [{"id": 55, "name": "原名称", "stealable_plots": 0}]
    payload = wire.blob(1, wire.uint(1, 55) + wire.blob(9, wire.uint(6, 4)))
    quota = wire.blob(2, wire.uint(1, 10003) + wire.uint(2, 100) + wire.uint(3, 100))
    service.client = SimpleNamespace(
        closed=False,
        fault=None,
        server_now=time.time,
        rpc=AsyncMock(side_effect=[quota, payload]),
    )
    try:
        await service.refresh_friend_hints(360)
        assert service.friends[0]["name"] == "原名称"
        assert service.friends[0]["stealable_plots"] == 4
        assert service.limits[10003] == {"used": 100, "maximum": 100}
        assert [call.args[1] for call in service.client.rpc.await_args_list] == [
            "AllLands", "GetGameFriends"
        ]
    finally:
        service.audit.close()


async def test_friend_maturity_appointment_updates_after_new_visit(tmp_path):
    service = FarmService(tmp_path)
    friend_id = 12345
    current = time.time()

    def visit(mature_at):
        return wire.blob(1, wire.uint(1, friend_id)) + wire.blob(
            2, land_payload(mature_at=mature_at, stealable=True, left_fruit=1)
        )

    service.client = SimpleNamespace(
        closed=False,
        fault=None,
        server_now=lambda: current,
        rpc=AsyncMock(side_effect=[visit(int(current + 80)), visit(int(current + 40))]),
    )
    policy = Policy(friend_steal=True, steal_priority=[friend_id], friend_interval=360)
    try:
        await service.read_friend_lands(friend_id)
        first = service.next_friend_due(policy)
        assert current + 82 <= first <= current + 86
        await service.read_friend_lands(friend_id)
        second = service.next_friend_due(policy)
        assert current + 42 <= second <= current + 46
        assert second < first
    finally:
        service.audit.close()


async def test_stale_stealable_plot_is_skipped_after_random_wait(tmp_path):
    service = FarmService(tmp_path)
    friend_id = 12345
    service.friends = [{"id": friend_id}]
    entries = 0

    async def rpc(_service, method, body=b""):
        nonlocal entries
        if method == "Enter":
            entries += 1
            return wire.blob(1, wire.uint(1, friend_id)) + wire.blob(
                2,
                land_payload(
                    mature_at=900,
                    stealable=True,
                    left_fruit=1 if entries == 1 else 0,
                ),
            )
        assert method == "Leave"
        return b""

    service.client = SimpleNamespace(
        gid=7,
        closed=False,
        fault=None,
        server_now=lambda: 1000,
        settings=Settings("test", random_action_delays=True),
        pace_write=AsyncMock(),
        rpc=AsyncMock(side_effect=rpc),
    )
    try:
        await service.friend_locked("steal", friend_id)
        methods = [call.args[1] for call in service.client.rpc.await_args_list]
        assert methods == ["Enter", "Leave", "Enter", "Leave"]
        service.client.pace_write.assert_awaited_once_with("Harvest")
        assert service.metrics["friend_actions"] == 0
        assert service.audit.db.execute("SELECT COUNT(*) FROM friend_effects").fetchone()[0] == 1
    finally:
        service.audit.close()


async def test_game_reported_stolen_crop_is_not_retried_for_same_growth(tmp_path):
    service = FarmService(tmp_path)
    friend_id = 12345
    service.friends = [{"id": friend_id}]

    async def rpc(_service, method, body=b""):
        if method == "Enter":
            return wire.blob(1, wire.uint(1, friend_id)) + wire.blob(
                2, land_payload(mature_at=900, stealable=True, left_fruit=1)
            )
        if method == "Harvest":
            raise RPCError("Harvest", 1001040, "果实被人摘走")
        assert method == "Leave"
        return b""

    service.client = SimpleNamespace(
        gid=7,
        closed=False,
        fault=None,
        server_now=lambda: 1000,
        rpc=AsyncMock(side_effect=rpc),
    )
    try:
        await service.friend_locked("steal", friend_id)
        await service.friend_locked("steal", friend_id)
        methods = [call.args[1] for call in service.client.rpc.await_args_list]
        assert methods.count("Harvest") == 1
        assert service.metrics["friend_actions"] == 0
        assert service.audit.db.execute("SELECT COUNT(*) FROM friend_effects").fetchone()[0] == 1
    finally:
        service.audit.close()


def test_daily_quotas_stop_both_local_full_and_game_rejected_actions(tmp_path):
    service = FarmService(tmp_path)
    now = time.time()
    service.client = SimpleNamespace(server_now=lambda: now)
    service.limits[10003] = {"used": 100, "maximum": 100}
    service.limits[10004] = {"used": 58, "maximum": 100}
    service.friend_stats.block(service.audit.account, "insects", "今日上限", day=day_key(now))
    try:
        quotas = service.friend_quotas(now)
        assert quotas["weeds"]["remaining"] == 0
        assert quotas["insects"]["remaining"] == 0
        assert quotas["insects"]["game_limit_reached"]
        assert service.limited_ids(10003, [1]) == []
        assert service.limited_ids(10004, [1]) == []
    finally:
        service.audit.close()
