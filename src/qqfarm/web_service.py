"""单账号常驻服务；网页只读缓存，全部复合游戏操作串行执行。"""

import asyncio
import base64
import json
import time
from dataclasses import replace
from pathlib import Path

import aiohttp
from cryptography.fernet import Fernet

from qqfarm import __version__, catalog, models, wire
from qqfarm.audit import Audit, InstanceLock, tag
from qqfarm.client import (
    FRIEND,
    ITEM,
    PLANT,
    SHOP,
    TASK,
    VISIT,
    GameClient,
    OperationPaused,
    RPCError,
    UncertainOutcome,
)
from qqfarm.config import FarmError, Settings
from qqfarm.engine import FarmEngine
from qqfarm.friend_stats import FriendStats, day_key
from qqfarm.web_policy import Policy

FRIEND_METHODS = {
    "steal": ("Harvest", 10008),
    "insects": ("PutInsects", 10004),
    "weeds": ("PutWeeds", 10003),
}


class FriendInteractionPaused(FarmError):
    """Only the affected friend action needs attention; own farming can continue."""


def friend_rejection(exc):
    return f"{exc} 原因未确认，此类互动已暂停；确认后可手动恢复。"


def friend_body(method, friend_id, ids):
    if method in {"PutInsects", "PutWeeds"}:
        # 当前1.14.2.15抓包核对：host在1，packed地块在2；不能套用旧版相反的字段。
        return wire.uint(1, friend_id) + wire.packed(2, ids) + wire.uint(3, 0) + wire.uint(4, 2)
    body = wire.packed(1, ids) + wire.uint(2, friend_id)
    if method == "Farming":
        return body + wire.uint(3, 0) + wire.uint(4, 2)
    return (
        body + wire.uint(3, 1) + wire.uint(4, 0) + wire.uint(5, 2) if method == "Harvest" else body
    )


class FarmService:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # ASGI的启动/关闭可以跨线程；游戏调度和数据库操作均在同一事件循环中串行执行。
        self.audit = Audit(self.root / "operations.sqlite3", check_same_thread=False)
        self.audit.db.execute(
            "CREATE TABLE IF NOT EXISTS friend_effects (identity TEXT PRIMARY KEY, at REAL)"
        )
        self.audit.db.commit()
        self.friend_stats = FriendStats(self.audit.db)
        self.audit.on_record = self.record
        policy = self.root / "policy.json"
        stored_policy = json.loads(policy.read_text()) if policy.exists() else {}
        # Mail support was retired in 0.6.1; retain every other saved setting.
        stored_policy.pop("auto_mail", None)
        self.policy = Policy.model_validate(stored_policy)
        key = self.root / "session-key"
        if not key.exists():
            key.write_bytes(Fernet.generate_key())
            key.chmod(0o600)
        self.cipher = Fernet(key.read_bytes())
        self.guard = asyncio.Lock()
        self.stop = asyncio.Event()
        self.policy_changed = asyncio.Event()
        self.reward_changed = asyncio.Event()
        self.task_check_at = 0.0
        self.client = self.session = self.loop = self.observer = self.lock = None
        self.running = False
        self.profile = None
        self.updated_at = 0
        self.next_check = 0
        self.note = "尚未连接，请导入本账号农场授权"
        self.mode = "offline"
        self.rows = {}
        self.seeds = []
        self.bag = []
        self.friends = []
        self.tasks = []
        self.limits = {}
        self.metrics = {
            "planted": 0,
            "harvested": 0,
            "coins_spent": 0,
            "sold": 0,
            "claimed": 0,
            "friend_actions": 0,
        }
        self.player = {"name": "未连接", "level": 0, "gold": 0, "fingerprint": ""}
        self.planted_this_round = set()
        self.manual_waiters = 0
        self.operation_epoch = 0
        self.operations = {}
        self.planting_decisions = []
        self.limits_day = day_key(time.time())
        self.seed_layouts = {}
        self.seed_layouts_version = ""
        layout_file = self.root / "seed-layouts.json"
        if layout_file.exists():
            try:
                stored = json.loads(layout_file.read_text())
                self.seed_layouts_version = stored["version"]
                self.seed_layouts = {
                    int(key): size
                    for key, size in stored["layouts"].items()
                    if type(size) is int
                    and size in {1, 2}
                    and catalog.plant(int(key)).get("layout_inferred")
                }
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.seed_layouts = {}
                self.audit.record("seed_layout", "unavailable", reason="invalid_local_cache")

    def record(self, operation, state, details):
        if state != "confirmed":
            return
        if operation == "plant":
            self.metrics["planted"] += 1
            self.planted_this_round.add(details["land"])
            seed, size = details.get("seed"), details.get("plot_size")
            if size in {1, 2} and len(details.get("occupied_lands", [])) == size**2:
                self.remember_seed_layout(seed, size)
        elif operation == "harvest":
            self.metrics["harvested"] += 1
        elif operation == "purchase":
            self.metrics["coins_spent"] += details["price"] * details["purchases"]
        elif operation == "sell":
            self.metrics["sold"] += details["count"]
        elif operation == "claim_task":
            self.metrics["claimed"] += 1
        elif operation.startswith("friend_"):
            self.metrics["friend_actions"] += 1
            self.friend_stats.update()

    def remember_seed_layout(self, seed, size):
        if not catalog.plant(seed).get("layout_inferred") or self.seed_layouts.get(seed) == size:
            return
        self.seed_layouts[seed] = size
        if self.client and hasattr(self.client, "settings"):
            self.seed_layouts_version = self.client.settings.client_version
        for row in self.seeds:
            if row["seed_id"] == seed:
                row.update(plot_size=size, layout_inferred=False, layout_verified=True)
        temporary = self.root / "seed-layouts.tmp"
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "version": self.seed_layouts_version,
                        "layouts": self.seed_layouts,
                    }
                )
            )
            temporary.replace(self.root / "seed-layouts.json")
        except OSError:
            self.audit.record("seed_layout", "unavailable", reason="cache_write_failed")

    def view(self):
        now = self.client.server_now() if self.client and not self.client.closed else time.time()
        if self.client:
            self.player["gold"] = self.client.gold
        statistics = self.friend_stats.snapshot(self.audit.account, now)
        return {
            "version": __version__,
            "capabilities": [
                "policy_patch",
                "friend_stats",
                "operation_progress",
                "priority_funds",
                "manual_friend_random",
                "inventory_seed_priority",
                "empty_seed_priority",
            ],
            "mode": self.mode,
            "running": self.running,
            "note": self.note,
            "updated_at": self.updated_at,
            "server_now": now,
            "next_check": self.next_check,
            "player": self.player,
            "metrics": self.metrics,
            "policy": self.policy.model_dump(),
            "lands": [
                {
                    "id": plot.id,
                    "name": (
                        self.rows[plot.master_id].plant_name
                        if plot.master_id in self.rows
                        else plot.plant_name
                    )
                    or "空地",
                    "status": "occupied" if plot.master_id else plot.status(now, allow_shared=True),
                    "mature_at": (
                        self.rows[plot.master_id].mature_at
                        if plot.master_id in self.rows
                        else plot.mature_at
                    ),
                    "shared": plot.shared,
                    "master_id": plot.master_id,
                    "occupied_ids": list(plot.occupied_ids),
                    "plot_size": plot.plot_size,
                }
                for plot in self.rows.values()
            ],
            "seeds": self.seeds,
            "inventory": self.bag,
            "friends": [
                {**friend, "today": statistics["friends"].get(tag(friend["id"]), {})}
                for friend in self.friends
            ],
            "tasks": self.tasks,
            "limits": self.limits,
            "friend_stats": statistics,
            "friend_blocks": self.friend_stats.blocks(self.audit.account, now),
            "friend_quota": self.friend_quotas(now),
            "operations": list(self.operations.values()),
            "planting_decisions": self.planting_decisions,
            "heartbeat": self.client.heartbeat_stats.snapshot() if self.client else None,
            "verification": {
                "shop": "本账号抓包及服务端",
                "fertilize": "本账号成功回包",
                "sell": "本账号旧抓包成功回包",
                "friend_weeds_insects": "本账号成功回包",
                "claim_tasks": "2026-10-11 单个及批量领取成功回包",
                "friend_help_steal": "2026-10-11 Farming 及采摘成功回包",
                "large_plants": "2026-10-11 两种四格作物播种及关联地块成功回包",
            },
        }

    def save_policy(self, value):
        selected = {p["seed_id"] for p in self.seeds}
        if selected and not set(value.seed_priority).issubset(selected):
            raise FarmError("种植优先级包含当前商店未提供的种子。")
        friend_ids = {f["id"] for f in self.friends}
        for kind in ("help", "steal", "insects", "weeds"):
            if friend_ids and not set(getattr(value, kind + "_priority")).issubset(friend_ids):
                raise FarmError("好友优先级包含当前好友列表之外的账号。")
        temporary = self.root / "policy.tmp"
        temporary.write_text(value.model_dump_json(), encoding="utf-8")
        temporary.replace(self.root / "policy.json")
        self.policy = value
        self.policy_changed.set()
        self.reward_changed.set()

    def reset_farm_state(self):
        self.rows, self.seeds, self.bag, self.friends, self.tasks = {}, [], [], [], []
        self.limits.clear()
        self.planting_decisions = []
        self.planted_this_round.clear()
        self.updated_at = self.next_check = self.task_check_at = 0
        self.profile = None
        self.audit.account = ""
        self.player = {"name": "未连接", "level": 0, "gold": 0, "fingerprint": ""}
        for key in self.metrics:
            self.metrics[key] = 0

    def sync_engine_state(self, engine):
        # A later rejection must not hide crops already confirmed earlier in the round.
        self.rows.update(engine.latest_rows)
        self.planting_decisions = engine.decisions
        if engine.land_payload:
            self.merge_limits(catalog.limits(engine.land_payload))
        if engine.inventory is not None:
            self.bag = catalog.inventory(engine.bag_payload)
            for seed in self.seeds:
                seed["inventory"] = engine.inventory.get(seed["seed_id"], 0)
            if engine.shop_payload:
                self.seeds = catalog.seeds(
                    engine.shop_payload, engine.inventory, layouts=self.seed_layouts
                )
        self.updated_at = time.time()

    def next_own_check(self, engine, operated_rows):
        # Fertilizer replies may have changed the maturity schedule after engine.round.
        latest = {key: self.rows[key] for key in operated_rows if key in self.rows}
        return time.time() + max(15, engine.next_delay(latest))

    async def login(self, profile):
        await self.pause()
        async with self.guard:
            await self.disconnect()
            self.reset_farm_state()
            if self.seed_layouts_version != profile["version"]:
                self.seed_layouts.clear()
                self.seed_layouts_version = profile["version"]
            self.mode, self.note = "connecting", "正在登录农场"
            try:
                self.lock = InstanceLock(self.root / "account.lock")
                self.lock.__enter__()
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15), trust_env=False
                )
                settings = Settings(
                    profile["code"],
                    client_version=profile["version"],
                    os_tag=profile.get("os", "Windows"),
                    data_dir=self.root,
                    wasm_path=self.root / "tsdk.wasm",
                    request_interval=1,
                    user_agent=profile.get("user_agent", "qqfarm-personal-web/1"),
                    origin=profile.get("origin", "https://gate-obt.nqf.qq.com"),
                    login_template=base64.b64decode(profile["template"], validate=True)
                    if profile.get("template")
                    else None,
                )
                self.client = GameClient(settings, self.session, self.audit)
                await self.client.connect()
                self.profile = {"friend_open_ids": profile.get("friend_open_ids", [])}
                self.friends = []
                self.client.settings = replace(settings, login_code="", login_template=None)
                self.player = {
                    "name": self.client.player_name,
                    "level": self.client.player_level,
                    "gold": self.client.gold,
                    "fingerprint": self.audit.account,
                }
                await self.refresh_locked()
                await self.client.wait(30)
                if not self.client.heartbeat_stats.ack_count:
                    raise FarmError("未确认心跳，尚未启用自动操作。")
                self.mode, self.note = "connected", "登录和心跳正常，自动操作尚未启动"
                # 仅加密保存本账号农场资料；启动服务时不会盲目重用失效code。
                (self.root / "farm-session.enc").write_bytes(
                    self.cipher.encrypt(json.dumps(profile).encode())
                )
                (self.root / "farm-session.enc").chmod(0o600)
                self.observer = asyncio.create_task(self.observe())
            except BaseException:
                await self.disconnect()
                self.mode, self.note = "offline", "连接失败，请重新获取本账号农场授权"
                raise

    async def observe(self):
        client = self.client
        while self.client is client and not client.disconnected.is_set():
            self.reward_changed.clear()
            delay = self.policy.reward_check_interval if self.running else self.reward_delay()
            waits = [
                asyncio.create_task(client.disconnected.wait()),
                asyncio.create_task(self.reward_changed.wait()),
            ]
            try:
                await asyncio.wait(waits, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiting in waits:
                    waiting.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
            if client.disconnected.is_set() or self.reward_changed.is_set():
                continue
            if not self.running:
                try:
                    async with self.guard:
                        if (
                            not self.running
                            and self.client is client
                            and not client.closed
                            and not client.fault
                        ):
                            interval = self.policy.reward_check_interval
                            if time.monotonic() >= self.task_check_at + interval:
                                await self.refresh_tasks_locked()
                except Exception as exc:
                    self.audit.record("task_status", "unavailable", error=type(exc).__name__)
        if self.client is client:
            self.running = False
            self.stop.set()
            self.mode, self.note = "offline", "农场连接已断开，已停止操作；请重新授权"

    def reward_delay(self):
        return max(
            1,
            self.task_check_at + self.policy.reward_check_interval - time.monotonic(),
        )

    async def refresh_locked(self, *, include_rewards=True):
        c = self.require_client()
        payload = await c.rpc(PLANT, "AllLands")
        rows = models.lands(payload)
        if len({plot.id for plot in rows}) != len(rows):
            raise FarmError("土地记录重复，已停止")
        self.rows = {plot.id: plot for plot in rows}
        self.limits.update(catalog.limits(payload))
        payload = await c.rpc(ITEM, "Bag")
        self.bag = catalog.inventory(payload)
        self.seeds = catalog.seeds(
            await c.rpc(SHOP, "ShopInfo", wire.uint(1, 2)),
            models.bag(payload),
            layouts=self.seed_layouts,
        )
        if include_rewards:
            await self.refresh_tasks_locked()
        ids = (self.profile or {}).get("friend_open_ids", [])
        try:
            if self.friends:
                payload = await c.rpc(
                    FRIEND,
                    "GetGameFriends",
                    wire.packed(1, [friend["id"] for friend in self.friends]),
                )
            elif ids:
                payload = await c.rpc(FRIEND, "SyncAll", b"".join(wire.text(2, i) for i in ids))
            else:
                self.audit.record("friend_list", "unavailable", reason="fresh_sync_required")
                payload = b""
            self.friends = [f for f in catalog.friends(payload) if f["id"] != c.gid]
        except RPCError:
            self.audit.record("friend_list", "unavailable")
        self.updated_at = time.time()

    async def refresh(self):
        async with self.guard:
            if time.time() - self.updated_at < 10:
                return
            await self.refresh_locked()

    def require_client(self):
        if not self.client or self.client.closed or self.client.fault:
            raise FarmError("请先连接农场，或重新获取授权。")
        return self.client

    async def start(self):
        self.require_client()
        if self.mode not in {"connected", "running"}:
            raise FarmError("请等待登录和心跳验证完成。")
        if self.loop and not self.loop.done():
            return
        self.stop.clear()
        self.policy_changed.clear()
        self.running, self.mode, self.note = True, "running", "自动种收已启动"
        self.reward_changed.set()
        self.loop = asyncio.create_task(self.schedule())

    async def pause(self):
        self.operation_epoch += 1
        self.running = False
        self.stop.set()
        self.reward_changed.set()
        if self.client:
            self.client.allow_writes = False
        if self.loop and self.loop is not asyncio.current_task():
            await asyncio.gather(self.loop, return_exceptions=True)
        self.loop = None
        if self.client and not self.client.closed and not self.client.fault:
            self.mode, self.note = "connected", "已暂停操作，心跳继续维护"

    async def disconnect(self):
        if self.observer:
            self.observer.cancel()
            await asyncio.gather(self.observer, return_exceptions=True)
            self.observer = None
        if self.client:
            await self.client.close()
            self.client = None
        if self.session:
            await self.session.close()
            self.session = None
        if self.lock:
            self.lock.__exit__()
            self.lock = None
        self.running = False
        self.next_check = 0
        self.mode = "offline"

    def choose_seed(self, policy):
        for seed_id in policy.seed_priority:
            goods = next((s for s in self.seeds if s["seed_id"] == seed_id and s["unlocked"]), None)
            if goods and (
                goods["inventory"] > 0
                or 0 < goods["price"] <= min(policy.purchase_budget, self.client.gold)
            ):
                return seed_id
        # 预算不足时仍能收获已有作物；FarmEngine会跳过不合格购买。
        return policy.seed_priority[0] if policy.seed_priority else 20002

    async def schedule(self):
        own_due, friend_due = 0, 0
        try:
            while self.running:
                if self.policy_changed.is_set():
                    self.policy_changed.clear()
                    own_due = friend_due = 0
                async with self.guard:
                    c = self.require_client()
                    c.allow_writes = self.running
                    policy = self.policy.model_copy(deep=True)
                    c.settings = replace(
                        c.settings,
                        random_action_delays=policy.random_action_delays,
                        action_delay_min=policy.action_delay_min,
                        action_delay_max=policy.action_delay_max,
                    )
                    now = time.time()
                    if now >= own_due:
                        self.roll_limits(c.server_now())
                        if policy.auto_care:
                            care_engine = FarmEngine(c.settings, c, self.audit, include_shared=True)
                            self.rows = await care_engine.get_lands()
                            self.merge_limits(catalog.limits(care_engine.land_payload))
                            await self.care_locked()
                        engine = FarmEngine(
                            replace(
                                c.settings,
                                seed_id=self.choose_seed(policy),
                                purchase_budget=policy.purchase_budget,
                                max_lands=policy.max_lands,
                                random_plant_order=policy.random_plant_order,
                                maturity_grace_seconds=policy.maturity_grace_seconds,
                            ),
                            c,
                            self.audit,
                            apply=True,
                            grouped_maturity=False,
                            include_shared=True,
                            seed_priority=policy.seed_priority,
                            purchase_mode=policy.purchase_mode,
                            fallback_radish=policy.fallback_radish,
                            checkpoint=self.yield_manual,
                            seed_layouts=self.seed_layouts,
                        )
                        self.planted_this_round.clear()
                        try:
                            operated_rows = await engine.round(
                                initial_rows=dict(self.rows) if policy.auto_care else None
                            )
                        finally:
                            self.sync_engine_state(engine)
                        if policy.fertilizer != "off":
                            fertilizer_id = 1011 if policy.fertilizer == "normal" else 1012
                            balance = sum(v["count"] for v in self.bag if v["id"] == fertilizer_id)
                            for plot in list(self.rows.values()):
                                if plot.id not in self.planted_this_round or balance <= 0:
                                    continue
                                # 每次种收轮仅在种子阶段施一次；不买肥料、不循环催熟。
                                current = [p for p in plot.phases if p.begin_time <= c.server_now()]
                                if (
                                    not current
                                    or max(current, key=lambda p: p.begin_time).phase != 1
                                ):
                                    continue
                                try:
                                    payload = await c.rpc(
                                        PLANT,
                                        "Fertilize",
                                        wire.packed(1, [plot.id]) + wire.uint(2, fertilizer_id),
                                    )
                                except RPCError as exc:
                                    self.audit.record(
                                        "fertilize", "skipped", land=plot.id, code=exc.code
                                    )
                                    break
                                FarmEngine.updated(self.rows, payload, plot.id)
                                reply = wire.parse(payload)
                                item = wire.data(reply, 3)
                                if item:
                                    balance = models.item(item)[1]
                                self.limits.update(catalog.limits(payload))
                                self.audit.record(
                                    "fertilize", "confirmed", land=plot.id, fertilizer=fertilizer_id
                                )
                        own_due = self.next_own_check(engine, operated_rows)
                        self.next_check = own_due
                        if policy.auto_sell:
                            await self.sell_locked(None)
                    if time.monotonic() >= self.task_check_at + policy.reward_check_interval:
                        if policy.auto_claim:
                            await self.claim_locked(None)
                        else:
                            await self.refresh_tasks_locked()
                    friends_enabled = any(
                        getattr(policy, "friend_" + kind)
                        for kind in ("help", "steal", "insects", "weeds")
                    )
                    c.allow_writes = False
                    c.settings = replace(c.settings, random_action_delays=False)
                    self.note = "自动运行中，按成熟时间检查；心跳独立维护"
                if friends_enabled and now >= friend_due:
                    await self.friend_cycle(policy)
                    friend_due = time.time() + policy.friend_interval
                deadlines = [own_due]
                if friends_enabled:
                    deadlines.append(friend_due)
                deadlines.append(time.time() + self.reward_delay())
                delay = max(1, min(deadlines) - time.time())
                waits = [
                    asyncio.create_task(self.stop.wait()),
                    asyncio.create_task(self.policy_changed.wait()),
                ]
                try:
                    await asyncio.wait(waits, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiting in waits:
                        waiting.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
        except OperationPaused:
            pass
        except Exception as exc:
            self.audit.record("scheduler", "stopped", error=type(exc).__name__)
            self.note = str(exc) if isinstance(exc, FarmError) else "调度异常，已暂停"
            if isinstance(exc, UncertainOutcome):
                await self.disconnect()
        finally:
            self.running = False
            self.reward_changed.set()
            if self.client:
                self.client.allow_writes = False
                self.client.settings = replace(self.client.settings, random_action_delays=False)
            self.mode = (
                "connected"
                if self.client and not self.client.closed and not self.client.fault
                else "offline"
            )

    async def yield_manual(self, rows):
        if not self.manual_waiters:
            return
        self.rows.update(rows)
        client = self.require_client()
        settings = client.settings
        client.allow_writes = False
        self.guard.release()
        try:
            await asyncio.sleep(0)
        finally:
            await self.guard.acquire()
        if not self.running or self.client is not client:
            raise OperationPaused("已暂停自动种收")
        rows.update({key: plot for key, plot in self.rows.items() if key in rows})
        client.settings = settings
        client.allow_writes = True

    def progress(self, operation_id, stage):
        if operation_id and operation_id in self.operations:
            self.operations[operation_id]["stage"] = stage

    async def manual(self, action, *, operation_id=None, **arguments):
        if self.manual_waiters >= 8:
            raise FarmError("等待中的操作较多，请稍后再提交。")
        admitted_at = self.operation_epoch
        if operation_id:
            self.operations[operation_id] = {
                "id": operation_id,
                "action": action,
                "stage": "等待当前游戏操作完成",
                "started_at": time.time(),
            }
        self.manual_waiters += 1
        try:
            await self.guard.acquire()
        except BaseException:
            self.manual_waiters -= 1
            self.operations.pop(operation_id, None)
            raise
        self.manual_waiters -= 1
        try:
            if admitted_at != self.operation_epoch:
                raise OperationPaused("已暂停，取消排队中的操作。")
            c = self.require_client()
            c.allow_writes = True
            c.settings = replace(
                c.settings,
                random_action_delays=self.policy.random_action_delays,
                action_delay_min=self.policy.action_delay_min,
                action_delay_max=self.policy.action_delay_max,
            )
            try:
                self.progress(operation_id, "正在执行")
                if action == "sell":
                    await self.sell_locked(arguments.get("items"))
                elif action == "claim":
                    await self.claim_locked(arguments.get("task_id"), operation_id=operation_id)
                elif action == "friend":
                    await self.friend_locked(arguments["kind"], arguments["friend_id"])
                elif action == "care":
                    engine = FarmEngine(c.settings, c, self.audit, include_shared=True)
                    self.rows = await engine.get_lands()
                    self.merge_limits(catalog.limits(engine.land_payload))
                    await self.care_locked()
                else:
                    raise FarmError("未知操作")
                # Reward replies already confirm state. Refresh only what the action changes.
                if action == "sell":
                    self.bag = catalog.inventory(await c.rpc(ITEM, "Bag"))
                self.updated_at = time.time()
            except UncertainOutcome:
                await self.disconnect()
                raise
            except RPCError as exc:
                if action == "friend":
                    self.friend_stats.block(
                        self.audit.account,
                        arguments["kind"],
                        friend_rejection(exc),
                    )
                raise
            finally:
                if self.client:
                    self.client.allow_writes = False
                    self.client.settings = replace(self.client.settings, random_action_delays=False)
        finally:
            self.guard.release()
            self.operations.pop(operation_id, None)

    async def sell_locked(self, requested):
        c = self.require_client()
        inventory = catalog.inventory(await c.rpc(ITEM, "Bag"))
        eligible = [r for r in inventory if r["sellable"] and r["count"] > 0]
        requested = (
            requested
            if requested is not None
            else [{"id": v["id"], "uid": v["uid"], "count": v["count"]} for v in eligible]
        )
        if not requested:
            return
        selected = []
        for value in requested:
            matches = [
                r
                for r in eligible
                if r["id"] == value["id"] and (value.get("uid") is None or r["uid"] == value["uid"])
            ]
            if len(matches) != 1 or not 0 < value["count"] <= matches[0]["count"]:
                raise FarmError("仅可出售现有收获物，数量不能超过库存。")
            selected.append({"id": value["id"], "uid": matches[0]["uid"], "count": value["count"]})
        if len({(x["id"], x["uid"]) for x in selected}) != len(selected):
            raise FarmError("出售物品重复")
        body = b"".join(
            wire.blob(
                1,
                wire.uint(1, x["id"]) + wire.uint(2, x["count"]) + wire.uint(6, x["uid"]),
            )
            for x in selected
        )
        response = wire.parse(await c.rpc(ITEM, "Sell", body))
        confirmed = {}
        for value in response.get(1, []):
            fields = wire.parse(value)
            key = (wire.number(fields, 1), wire.number(fields, 6))
            if key in confirmed:
                raise UncertainOutcome("出售回包含重复物品，已暂停。")
            confirmed[key] = wire.number(fields, 2)
        if any(confirmed.get((x["id"], x["uid"])) != x["count"] for x in selected):
            raise UncertainOutcome("出售回包未确认数量，已暂停，不会重试。")
        self.audit.record(
            "sell", "confirmed", count=sum(x["count"] for x in requested), items=len(requested)
        )

    async def refresh_tasks_locked(self):
        c = self.require_client()
        # 记录查询尝试，查询失败也不紧密重试；手动刷新和领奖仍可立即核对。
        self.task_check_at = time.monotonic()
        self.tasks = catalog.tasks(await c.rpc(TASK, "TaskInfo"))

    async def claim_locked(self, task_id, *, operation_id=None):
        c = self.require_client()
        self.progress(operation_id, "查询任务完成与领取状态")
        await self.refresh_tasks_locked()
        candidates = [
            t
            for t in self.tasks
            if t["claimable"] and (t["type"] == 2 if task_id is None else t["id"] == task_id)
        ]
        if task_id is not None and not candidates:
            raise FarmError("任务未完成或奖励已领取。")
        if not candidates:
            return
        ids = [task["id"] for task in candidates]
        method = "BatchClaimTaskReward" if len(ids) > 1 else "ClaimTaskReward"
        body = wire.packed(1, ids) if len(ids) > 1 else wire.uint(1, ids[0]) + wire.uint(2, 0)
        self.progress(operation_id, f"领取 {len(ids)} 项任务奖励")
        response = wire.parse(await c.rpc(TASK, method, body))
        self.progress(operation_id, "核对领取回包")
        updated = catalog.tasks(wire.data(response, 2), nested=False)
        confirmed = {task["id"] for task in updated if task["claimed"]}
        if not set(ids).issubset(confirmed):
            raise UncertainOutcome("领取回包未确认全部任务状态，已暂停，不能自动重领。")
        by_id = {task["id"]: task for task in self.tasks}
        by_id.update({task["id"]: task for task in updated})
        self.tasks = list(by_id.values())
        for task in candidates:
            self.audit.record("claim_task", "confirmed", task=task["id"])

    def care_ids(self, rows, now):
        active = [
            plot
            for plot in rows.values()
            if plot.status(now, allow_shared=True) in {"growing", "mature"}
        ]
        wanted = {
            10007: [plot.id for plot in active if plot.dry_num > 0],
            10005: [plot.id for plot in active if plot.weed_owners],
            10006: [plot.id for plot in active if plot.insect_owners],
        }
        permitted = {kind: set(self.limited_ids(kind, ids)) for kind, ids in wanted.items()}
        return [
            plot.id
            for plot in active
            if any(plot.id in ids for ids in wanted.values())
            and all(
                plot.id not in ids or plot.id in permitted[kind] for kind, ids in wanted.items()
            )
        ]

    @staticmethod
    def update_farming(rows, payload, ids):
        # Farming 对同一地块可按浇水/除草/除虫分别返回更新；最后一条为最终状态。
        by_id = {plot.id: plot for plot in models.lands(payload)}
        if not set(ids).issubset(by_id):
            raise UncertainOutcome("照料回包没有完整目标状态，已停止，不重复提交。")
        if any(
            by_id[key].dry_num or by_id[key].weed_owners or by_id[key].insect_owners for key in ids
        ):
            raise UncertainOutcome("照料回包未确认目标问题清除，已停止，不重复提交。")
        rows.update({key: value for key, value in by_id.items() if key in rows})

    async def care_locked(self):
        c = self.require_client()
        ids = self.care_ids(self.rows, c.server_now())
        if not ids:
            return
        body = wire.packed(1, ids) + wire.uint(2, c.gid) + wire.uint(3, 0) + wire.uint(4, 0)
        response = await c.rpc(PLANT, "Farming", body)
        confirmed = await self.confirm_care(self.rows, response, ids)
        self.merge_limits(catalog.limits(response))
        self.audit.record("care", "confirmed" if confirmed else "received", plots=ids)

    async def read_friend_lands(self, friend_id, *, revisit=False):
        c = self.require_client()
        if revisit:
            await c.rpc(VISIT, "Leave", wire.uint(1, friend_id) + wire.uint(2, 0))
        payload = await c.rpc(
            VISIT,
            "Enter",
            wire.uint(1, friend_id) + wire.uint(2, 2) + wire.uint(3, 0) + wire.blob(7, b""),
        )
        fields = wire.parse(payload)
        if wire.number(wire.parse(wire.data(fields, 1)), 1) != friend_id:
            raise FarmError("好友农场身份与请求不匹配，未执行互动。")
        return {plot.id: plot for plot in (models.land(v) for v in fields.get(2, []))}

    async def confirm_care(self, rows, payload, ids, *, friend_id=None):
        try:
            self.update_farming(rows, payload, ids)
            return True
        except UncertainOutcome:
            # Successful Farming replies may omit untouched/stale targets. Query once;
            # never send a second write to determine what happened.
            self.audit.record(
                "care_verification",
                "started",
                plots=ids,
                returned_plots=[p.id for p in models.lands(payload)],
                friend=tag(friend_id) if friend_id is not None else None,
            )
        if friend_id is not None:
            latest = await self.read_friend_lands(friend_id, revisit=True)
        else:
            latest_payload = await self.require_client().rpc(PLANT, "AllLands")
            latest = {p.id: p for p in models.lands(latest_payload)}
            self.merge_limits(catalog.limits(latest_payload))
        rows.update(latest)
        unresolved = [
            key
            for key in ids
            if key not in latest
            or latest[key].dry_num
            or latest[key].weed_owners
            or latest[key].insect_owners
        ]
        if unresolved:
            reason = "照料回包未完整确认，复查后仍无法核实全部目标；已暂停，不重复提交。"
            self.audit.record("care_verification", "unknown", plots=unresolved)
            if friend_id is not None:
                self.friend_stats.block(self.audit.account, "help", reason)
                raise FriendInteractionPaused(reason)
            raise UncertainOutcome(reason)
        self.audit.record(
            "care_verification",
            "received",
            plots=ids,
            reason="复查确认目标已无需照料；未重发请求，也不计为本助手确认的帮助次数。",
        )
        return False

    async def friend_cycle(self, policy):
        for kind in ("help", "steal", "insects", "weeds"):
            if not getattr(policy, "friend_" + kind):
                continue
            if kind in self.friend_stats.blocks(self.audit.account, self.client.server_now()):
                continue
            if kind in {"insects", "weeds"} and not self.limited_ids(FRIEND_METHODS[kind][1], [0]):
                continue
            priority = list(getattr(policy, kind + "_priority"))
            if policy.friends_scope == "all":
                priority += [f["id"] for f in self.friends if f["id"] not in priority]
            for gid in priority[: policy.friends_per_cycle]:
                if gid == self.client.gid:
                    continue
                if not self.running:
                    raise OperationPaused("已暂停好友操作")
                try:
                    async with self.guard:
                        c = self.require_client()
                        if not self.running:
                            raise OperationPaused("已暂停好友操作")
                        c.allow_writes = True
                        c.settings = replace(
                            c.settings,
                            random_action_delays=policy.random_action_delays,
                            action_delay_min=policy.action_delay_min,
                            action_delay_max=policy.action_delay_max,
                        )
                        try:
                            await self.friend_locked(kind, gid)
                        finally:
                            c.allow_writes = False
                            c.settings = replace(c.settings, random_action_delays=False)
                except RPCError as exc:
                    self.friend_stats.block(self.audit.account, kind, friend_rejection(exc))
                    self.audit.record("friend_action", "skipped", friend=tag(gid), code=exc.code)
                    break
                except FriendInteractionPaused:
                    break
                if self.next_check and time.time() >= self.next_check:
                    return

    def roll_limits(self, now):
        day = day_key(now)
        if day != self.limits_day:
            self.limits.clear()
            self.limits_day = day

    def merge_limits(self, values):
        self.roll_limits(self.client.server_now())
        for key, value in values.items():
            old = self.limits.get(key, {})
            self.limits[key] = {**value, "used": max(old.get("used", 0), value["used"])}

    def friend_quotas(self, now):
        self.roll_limits(now)
        totals = self.friend_stats.snapshot(self.audit.account, now)["totals"]
        result = {}
        for kind in ("insects", "weeds"):
            server = self.limits.get(FRIEND_METHODS[kind][1], {})
            maximum = min(100, server.get("maximum") or 100)
            used = max(totals[kind]["plots"], server.get("used", 0))
            result[kind] = {
                "used": used,
                "maximum": maximum,
                "remaining": max(0, maximum - used),
                "source": "游戏回包与本助手记录" if server else "本助手保守上限",
            }
        return result

    def limited_ids(self, operation_id, ids):
        if operation_id in {10004, 10003}:
            kind = "insects" if operation_id == 10004 else "weeds"
            quota = self.friend_quotas(self.client.server_now())[kind]
            return ids[: quota["remaining"]]
        limit = self.limits.get(operation_id)
        if limit and limit["maximum"] > 0:
            return ids[: max(0, limit["maximum"] - limit["used"])]
        return ids

    async def friend_locked(self, kind, friend_id):
        c = self.require_client()
        blocked = self.friend_stats.blocks(self.audit.account, c.server_now()).get(kind)
        if blocked:
            raise FarmError(f"此类好友操作已暂停：{blocked}")
        if kind in {"insects", "weeds"} and not self.limited_ids(FRIEND_METHODS[kind][1], [0]):
            return
        if (
            kind not in {"help", "steal", "insects", "weeds"}
            or friend_id == c.gid
            or friend_id not in {f["id"] for f in self.friends}
        ):
            raise FarmError("好友或互动类型无效")
        try:
            rows = await self.read_friend_lands(friend_id)
            actions = (
                [
                    (
                        "Farming",
                        None,
                        lambda plot: bool(plot.dry_num or plot.weed_owners or plot.insect_owners),
                    ),
                ]
                if kind == "help"
                else [(FRIEND_METHODS[kind][0], FRIEND_METHODS[kind][1], None)]
            )
            for method, limit_id, selector in actions:
                ids = []
                for plot in rows.values():
                    state = plot.status(c.server_now(), allow_shared=True)
                    if kind == "help":
                        eligible = state in {"growing", "mature"} and selector(plot)
                    elif kind == "steal":
                        eligible = state == "mature" and plot.stealable and plot.left_fruit > 0
                    else:
                        eligible = state == "growing" and not (
                            plot.insect_owners if kind == "insects" else plot.weed_owners
                        )
                    identity = (
                        f"{c.gid}:{friend_id}:{plot.id}:{plot.plant_id}:{plot.mature_at}:{method}"
                    )
                    if (
                        eligible
                        and not self.audit.db.execute(
                            "SELECT 1 FROM friend_effects WHERE identity=?", (identity,)
                        ).fetchone()
                    ):
                        ids.append(plot.id)
                if method == "Farming":
                    allowed = set(self.care_ids(rows, c.server_now()))
                    ids = [key for key in ids if key in allowed]
                else:
                    ids = self.limited_ids(limit_id, ids)
                if not ids:
                    continue
                identities = {
                    key: f"{c.gid}:{friend_id}:{key}:{rows[key].plant_id}:"
                    f"{rows[key].mature_at}:{method}"
                    for key in ids
                }
                care_counts = (
                    {
                        "water": sum(bool(rows[key].dry_num) for key in ids),
                        "weeds": sum(bool(rows[key].weed_owners) for key in ids),
                        "insects": sum(bool(rows[key].insect_owners) for key in ids),
                    }
                    if kind == "help"
                    else {}
                )
                try:
                    response = await c.rpc(PLANT, method, friend_body(method, friend_id, ids))
                except RPCError as exc:
                    if kind not in {"help", "steal"} or exc.method != method:
                        raise
                    latest = await self.read_friend_lands(friend_id, revisit=True)
                    # Missing land records are not evidence that the action is safe.
                    changed = [
                        key
                        for key in ids
                        if key in latest
                        and (
                            (latest[key].plant_id, latest[key].mature_at)
                            != (rows[key].plant_id, rows[key].mature_at)
                            or (
                                kind == "help"
                                and not (
                                    latest[key].dry_num
                                    or latest[key].weed_owners
                                    or latest[key].insect_owners
                                )
                            )
                            or (
                                kind == "steal"
                                and not (
                                    latest[key].status(c.server_now(), allow_shared=True)
                                    == "mature"
                                    and latest[key].stealable
                                    and latest[key].left_fruit > 0
                                )
                            )
                        )
                    ]
                    rows = latest
                    if not changed or not set(ids).issubset(latest):
                        raise exc
                    self.audit.record(
                        "friend_" + kind,
                        "skipped",
                        friend=tag(friend_id),
                        method=method,
                        code=exc.code,
                        message=exc.message,
                        plots=ids,
                        changed_plots=changed,
                        reason="拒绝后复查发现目标状态已变化，本次跳过，未重发；错误码含义未确定。",
                    )
                    continue
                if method == "Farming":
                    confirmed = await self.confirm_care(rows, response, ids, friend_id=friend_id)
                    self.merge_limits(catalog.limits(response))
                    if not confirmed:
                        continue
                else:
                    FarmEngine.updated_batch(rows, response, ids)
                self.merge_limits(catalog.limits(response, 4 if method == "Harvest" else 2))
                for plot in ids:
                    plot = rows[plot]
                    self.audit.db.execute(
                        "INSERT OR REPLACE INTO friend_effects VALUES (?,?)",
                        (identities[plot.id], time.time()),
                    )
                self.audit.db.commit()
                self.audit.record(
                    "friend_" + kind,
                    "confirmed",
                    friend=tag(friend_id),
                    method=method,
                    plots=ids,
                    care_counts=care_counts,
                )
            for friend in self.friends:
                if friend["id"] == friend_id:
                    friend["stealable_plots"] = sum(
                        plot.status(c.server_now(), allow_shared=True) == "mature"
                        and plot.stealable
                        and plot.left_fruit > 0
                        for plot in rows.values()
                    )
                    friend["checked_at"] = time.time()
        except RPCError as exc:
            self.friend_stats.block(self.audit.account, kind, friend_rejection(exc))
            raise
        finally:
            try:
                await c.rpc(VISIT, "Leave", wire.uint(1, friend_id) + wire.uint(2, 0))
            except RPCError as exc:
                # A refused Leave must not replace the original action/verification error.
                self.audit.record(
                    "friend_visit_leave",
                    "rejected",
                    friend=tag(friend_id),
                    code=exc.code,
                    message=exc.message,
                )

    async def close(self):
        await self.pause()
        async with self.guard:
            await self.disconnect()
        self.audit.close()
