import asyncio
import random
import re
import time
from contextlib import suppress
from urllib.parse import urlencode

import aiohttp

from qqfarm import models, wire
from qqfarm.audit import tag
from qqfarm.config import FarmError, Settings
from qqfarm.crypto import GameCrypto
from qqfarm.heartbeat import ACK_TIMEOUT, INTERVAL, HeartbeatStats, next_deadline

USER = "gamepb.userpb.UserService"
PLANT = "gamepb.plantpb.PlantService"
SHOP = "gamepb.shoppb.ShopService"
ITEM = "gamepb.itempb.ItemService"
FRIEND = "gamepb.friendpb.FriendService"
VISIT = "gamepb.visitpb.VisitService"
TASK = "gamepb.taskpb.TaskService"
ALLOWED = {
    (USER, "Login"),
    (USER, "PlayerLogin"),
    (USER, "Heartbeat"),
    (PLANT, "AllLands"),
    (ITEM, "Bag"),
    (SHOP, "ShopInfo"),
    (SHOP, "BuyGoods"),
    (PLANT, "Harvest"),
    (PLANT, "RemovePlant"),
    (PLANT, "Plant"),
    (PLANT, "Fertilize"),
    (PLANT, "Farming"),
    (PLANT, "WaterLand"),
    (PLANT, "WeedOut"),
    (PLANT, "Insecticide"),
    (PLANT, "PutInsects"),
    (PLANT, "PutWeeds"),
    (FRIEND, "GetAll"),
    (FRIEND, "SyncAll"),
    (FRIEND, "GetGameFriends"),
    (VISIT, "Enter"),
    (VISIT, "Leave"),
    (TASK, "TaskInfo"),
    (TASK, "ClaimTaskReward"),
    (TASK, "BatchClaimTaskReward"),
    (ITEM, "Sell"),
}
WRITES = {
    "BuyGoods",
    "Harvest",
    "RemovePlant",
    "Plant",
    "Fertilize",
    "Farming",
    "WaterLand",
    "WeedOut",
    "Insecticide",
    "PutInsects",
    "PutWeeds",
    "ClaimTaskReward",
    "BatchClaimTaskReward",
    "Sell",
}


class RPCError(FarmError):
    def __init__(self, method, code, message="", *, body_bytes=0):
        self.method, self.code = method, code
        self.message, self.body_bytes = message, body_bytes
        explanation = f"游戏提示：{message}" if message else ""
        super().__init__(f"{method} 被服务器拒绝（错误码 {code}）。" + explanation)


def error_message(text, settings):
    """Keep the optional game explanation without persisting authorization values."""
    for secret in (settings.login_code, *settings.friend_open_ids):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    text = re.sub(
        r"(?i)((?:code|token|cookie|authorization|openid)\s*[=:]\s*)[^\s&,;]+",
        r"\1[已隐藏]",
        text,
    )
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [已隐藏]", text)
    return " ".join(text.split())[:300]


class UncertainOutcome(FarmError):
    pass


class OperationPaused(FarmError):
    pass


class GameClient:
    def __init__(self, settings: Settings, session, audit, *, allow_writes=False, crypto=None):
        self.settings, self.session, self.audit = settings, session, audit
        self.crypto = crypto or GameCrypto(settings.wasm_path)
        self.write_paused = asyncio.Event()
        self.allow_writes = allow_writes
        self.ws = self.reader = self.heartbeat = None
        self.pending = {}
        self.lock = asyncio.Lock()
        self.heartbeat_lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.heartbeat_stats = HeartbeatStats()
        self.seq, self.server_seq, self.gid = 0, 0, 0
        self.last_sent = 0.0
        self.anchor_server, self.anchor_mono = time.time(), time.monotonic()
        self.closed = False
        self.stop_requested = False
        self.fault = None
        self.disconnect_reason = None
        self.disconnected = asyncio.Event()
        self.gold = 0
        self.player_name = ""
        self.player_level = 0

    @property
    def allow_writes(self):
        return self._allow_writes

    @allow_writes.setter
    def allow_writes(self, value):
        self._allow_writes = bool(value)
        if value:
            self.write_paused.clear()
        else:
            self.write_paused.set()

    async def pace_write(self, method):
        seconds = random.uniform(self.settings.action_delay_min, self.settings.action_delay_max)
        self.audit.record("action_delay", "waiting", method=method, seconds=round(seconds, 3))
        paused = asyncio.create_task(self.write_paused.wait())
        disconnected = asyncio.create_task(self.disconnected.wait())
        try:
            await asyncio.wait(
                [paused, disconnected], timeout=seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if self.disconnected.is_set():
                raise FarmError("等待期间游戏断开，已停止操作。")
            if self.write_paused.is_set():
                raise OperationPaused("已暂停，未发送等待中的操作。")
        finally:
            paused.cancel()
            disconnected.cancel()
            await asyncio.gather(paused, disconnected, return_exceptions=True)

    def server_now(self):
        return self.anchor_server + time.monotonic() - self.anchor_mono

    async def wait(self, seconds):
        """等待成熟期间若连接失效，立即结束，不继续购买或种植。"""
        try:
            await asyncio.wait_for(self.disconnected.wait(), timeout=seconds)
        except TimeoutError:
            return
        raise FarmError("游戏会话已断开，已停止调度。")

    def sync_clock(self, stamp):
        stamp = stamp / 1000 if stamp >= 1_000_000_000_000 else stamp
        if not 1_500_000_000 < stamp < 2_200_000_000:
            raise wire.ProtocolError("服务器时间超出可识别范围。")
        self.anchor_server, self.anchor_mono = stamp, time.monotonic()

    async def connect(self):
        query = urlencode(
            {
                "platform": self.settings.platform,
                "os": self.settings.os_tag,
                "ver": self.settings.client_version,
                "code": self.settings.login_code,
            }
        )
        try:
            self.ws = await self.session.ws_connect(
                "wss://gate-obt.nqf.qq.com/prod/ws?" + query,
                headers={"User-Agent": self.settings.user_agent, "Origin": self.settings.origin},
                max_msg_size=wire.MAX_MESSAGE_BYTES,
                timeout=aiohttp.ClientWSTimeout(ws_close=3),
            )
        except aiohttp.WSServerHandshakeError as exc:
            raise FarmError(
                f"游戏连接未接受（HTTP {exc.status}）。登录 code 可能已使用或失效，请重新导入。"
            ) from None
        self.reader = asyncio.create_task(self.receive(), name="farm-reader")
        body = self.settings.login_template
        if body is None:
            if self.settings.login_method != "Login":
                raise FarmError("新版登录缺少 SAZ 模板，请重新导入当前抓包。")
            device = wire.text(1, self.settings.client_version) + wire.text(2, self.settings.os_tag)
            body = wire.blob(5, device)
        else:
            fields = wire.parse(body)
            if self.settings.login_method == "Login":
                device = wire.parse(wire.data(fields, 5))
                if wire.string(device, 1) != self.settings.client_version:
                    raise FarmError("登录模板与客户端版本不一致，请重新导入当前抓包。")
            elif self.settings.login_method == "PlayerLogin":
                device = wire.parse(wire.data(fields, 3))
                if (
                    wire.string(device, 1) != self.settings.client_version
                    or wire.string(device, 2) != self.settings.os_tag
                ):
                    raise FarmError("新版登录模板的版本或系统信息不一致，请重新导入当前抓包。")
            else:
                raise FarmError("SAZ 中的登录接口不受支持，请重新导入当前抓包。")
        reply = wire.parse(await self.rpc(USER, self.settings.login_method, body))
        basic = wire.parse(wire.data(reply, 1))
        self.gid, self.gold = wire.number(basic, 1), wire.number(basic, 5)
        self.player_name = wire.string(basic, 2)[:80]
        self.player_level = wire.number(basic, 3)
        if not self.gid:
            raise FarmError("登录回复没有有效玩家标识。")
        self.audit.account = tag(self.gid)
        server_time = wire.number(reply, 3)
        if server_time:
            self.sync_clock(server_time)
        elif self.settings.login_method != "PlayerLogin":
            self.sync_clock(server_time)
        self.audit.record(
            "login",
            "success",
            level=wire.number(basic, 3),
            gold=self.gold,
            version=self.settings.client_version,
        )
        self.heartbeat_stats = HeartbeatStats()
        self.heartbeat = asyncio.create_task(self.keepalive(), name="farm-heartbeat")

    async def rpc(self, service, method, body=b"", *, due_at=None):
        if (service, method) not in ALLOWED:
            raise FarmError("不支持的农场操作。")
        if method in WRITES and not self.allow_writes:
            raise OperationPaused("当前只读模式不会购买、种植或收获。")
        # 业务操作仍逐个等回包；心跳可与一个业务请求同时等待回应。
        # 仅发送阶段共用锁，避免序号混乱及写帧重叠。
        async with self.heartbeat_lock if method == "Heartbeat" else self.lock:
            if self.closed or self.fault or self.ws is None or self.ws.closed:
                raise FarmError("游戏会话已断开，已停止操作。")
            future = asyncio.get_running_loop().create_future()
            seq = None
            operation = "rpc_" + method
            sent = False
            try:
                if method in WRITES and self.settings.random_action_delays:
                    # 等待在发送锁和回包超时之外；心跳保持独立，暂停可立即唤醒。
                    await self.pace_write(method)
                # 包含排队、发送背压及回包等待，不能只对 future 设置超时。
                async with asyncio.timeout(ACK_TIMEOUT):
                    async with self.send_lock:
                        minimum_interval = self.settings.request_interval
                        if method in WRITES and self.settings.random_action_delays:
                            minimum_interval = min(minimum_interval, self.settings.action_delay_min)
                        delay = minimum_interval - (time.monotonic() - self.last_sent)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        if self.fault or self.closed or self.ws.closed:
                            raise FarmError("游戏会话已断开，已停止操作。")
                        if method in WRITES and not self.allow_writes:
                            raise OperationPaused("已暂停，未发送新种收操作。")
                        self.seq += 1
                        seq = self.seq
                        self.pending[seq] = (service, method, future)
                        self.audit.record(operation, "started", seq=seq, write=method in WRITES)
                        encoded = wire.encode_gate(
                            service, method, seq, self.server_seq, self.crypto.encrypt(body)
                        )
                        self.last_sent = sent_at = time.monotonic()
                        sent = True
                        if method == "Heartbeat":
                            self.heartbeat_stats.sent(sent_at, due_at or sent_at)
                        await self.ws.send_bytes(encoded)
                    result = await future
                if method == "Heartbeat":
                    received_at = time.monotonic()
                    # 校验业务回应，不把任意消息或 Ping/Pong 当作心跳成功。
                    stamp = wire.number(wire.parse(result), 1)
                    self.sync_clock(stamp)
                    self.heartbeat_stats.acknowledged(sent_at, received_at)
                    self.audit.record(
                        "heartbeat",
                        "acknowledged",
                        seq=seq,
                        rtt_ms=round((received_at - sent_at) * 1000, 2),
                        schedule_late_ms=round(max(0, sent_at - (due_at or sent_at)) * 1000, 2),
                        server_time_ms=stamp,
                    )
                self.audit.record(operation, "success", seq=seq)
                return result
            except OperationPaused:
                self.audit.record(operation, "cancelled", seq=seq, reason="writes_paused")
                raise
            except RPCError as exc:
                self.audit.record(
                    operation,
                    "rejected",
                    seq=seq,
                    code=exc.code,
                    message=exc.message,
                    reply_body_bytes=exc.body_bytes,
                )
                raise
            except (TimeoutError, aiohttp.ClientError, FarmError, ConnectionError) as exc:
                if self.stop_requested and not sent:
                    self.audit.record(operation, "cancelled", seq=seq, reason="local_stop")
                    raise FarmError("收到停止请求，未发送新操作。") from None
                unknown = sent and method in WRITES
                self.audit.record(
                    operation, "unknown" if unknown else "failed", seq=seq, error=type(exc).__name__
                )
                if unknown:
                    raise UncertainOutcome(
                        f"{method} 已发送但结果无法确认，已停止，不会自动重发。"
                    ) from None
                raise FarmError(f"{method} 未完成，已停止当前会话。") from None
            finally:
                self.pending.pop(seq, None)
                if not future.done():
                    future.cancel()

    async def receive(self):
        try:
            async for frame in self.ws:
                if frame.type != aiohttp.WSMsgType.BINARY:
                    if frame.type == aiohttp.WSMsgType.ERROR:
                        raise FarmError("游戏连接异常。")
                    continue
                packet = wire.decode_gate(frame.data)
                self.server_seq = max(self.server_seq, packet["server_seq"])
                if packet["type"] == 2:
                    entry = self.pending.get(packet["seq"])
                    if not entry or entry[2].done():
                        continue
                    service, method, future = entry
                    if (packet["service"], packet["method"]) != (service, method):
                        raise wire.ProtocolError("回复服务或方法与请求不匹配。")
                    if packet["error"]:
                        future.set_exception(
                            RPCError(
                                method,
                                packet["error"],
                                error_message(packet["error_message"], self.settings),
                                body_bytes=len(packet["body"]),
                            )
                        )
                    else:
                        future.set_result(packet["body"])
                elif packet["type"] == 3:
                    self.notify(packet["body"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fault = FarmError("消息接收失败，已停止会话。")
            self.disconnect_reason = self.disconnect_reason or "receive_error"
            self.audit.record("connection", "failed", error=type(exc).__name__)
        finally:
            self.fault = self.fault or FarmError("游戏会话断开。")
            self.disconnect_reason = self.disconnect_reason or (
                "local_stop" if self.closed else "remote_closed"
            )
            self.disconnected.set()
            for _, _, future in self.pending.values():
                if not future.done():
                    future.set_exception(self.fault)
            self.audit.record(
                "connection",
                "closed",
                reason=self.disconnect_reason,
                close_code=getattr(self.ws, "close_code", None),
                pending_replies=len(self.pending),
            )

    def notify(self, payload):
        event = wire.parse(payload)
        name = wire.string(event, 1)
        if "Kickout" in name:
            self.disconnect_reason = "server_kickout"
            raise FarmError("游戏要求下线。")
        if name == "gamepb.itempb.ItemNotify":
            fields = wire.parse(wire.data(event, 2))
            for value in fields.get(1, []):
                change = wire.parse(value)
                item_id, count = models.item(wire.data(change, 1))
                if item_id == 1001:
                    self.gold = count
                delta = wire.number(change, 2)
                if delta >= 1 << 63:
                    delta -= 1 << 64
                self.audit.record(
                    "item_change", "received", item_id=item_id, count=count, delta=delta
                )

    async def keepalive(self, *, interval=INTERVAL):
        try:
            due = time.monotonic() + interval
            while not self.closed:
                await self.wait(max(0, due - time.monotonic()))
                # 字段 3=0 已在当前客户端抓包中核对，语义尚未确认。
                payload = (
                    wire.uint(1, self.gid)
                    + wire.text(2, self.settings.client_version)
                    + wire.uint(3, 0)
                )
                await self.rpc(USER, "Heartbeat", payload, due_at=due)
                due = next_deadline(due, time.monotonic(), interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.stop_requested:
                self.disconnect_reason = "local_stop"
                self.audit.record("heartbeat", "stopped", reason="local_stop")
                return
            self.fault = self.fault or FarmError("心跳未完成，已停止会话。")
            self.disconnect_reason = self.disconnect_reason or "heartbeat_failed"
            self.disconnected.set()
            self.audit.record("heartbeat", "failed", error=type(exc).__name__)
            await self.ws.close()

    async def close(self):
        self.closed = True
        self.disconnected.set()
        if self.heartbeat:
            self.heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await self.heartbeat
        try:
            # 先发送关闭握手，避免先取消 receive 让库标记为异常关闭 1006。
            if self.ws:
                await self.ws.close()
                self.audit.record(
                    "transport",
                    "closed",
                    close_code=getattr(self.ws, "close_code", None),
                    local_stop=True,
                )
        finally:
            if self.reader:
                self.reader.cancel()
                with suppress(asyncio.CancelledError):
                    await self.reader
