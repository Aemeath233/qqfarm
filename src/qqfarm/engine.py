import math
import random

from qqfarm import catalog, models, wire
from qqfarm.client import ITEM, PLANT, SHOP, UncertainOutcome
from qqfarm.config import FarmError


class FarmEngine:
    def __init__(
        self,
        settings,
        client,
        audit,
        *,
        apply=False,
        grouped_maturity=False,
        include_shared=False,
        seed_priority=None,
        purchase_mode="budget",
        fallback_radish=False,
        checkpoint=None,
        seed_layouts=None,
    ):
        self.settings, self.client, self.audit, self.apply = settings, client, audit, apply
        self.grouped_maturity = grouped_maturity
        self.include_shared = include_shared
        self.seed_priority = list(seed_priority) if seed_priority is not None else None
        self.purchase_mode = purchase_mode
        self.fallback_radish = fallback_radish
        self.checkpoint = checkpoint
        self.seed_layouts = dict(seed_layouts or {})
        self.decisions = []
        self.inventory = None
        self.shop_payload = None
        self.land_payload = None
        self.bag_payload = None
        self.latest_rows = {}

    async def get_lands(self):
        self.land_payload = await self.client.rpc(PLANT, "AllLands")
        rows = models.lands(self.land_payload)
        # 单账号界限：原地块 ID 去重，解锁/合并/共享属性均由服务器返回。
        if len({land.id for land in rows}) != len(rows):
            raise FarmError("土地列表包含重复 ID，已停止操作。")
        by_id = {land.id: land for land in rows}
        selected = {}
        for plot in rows:
            if plot.status(self.client.server_now(), allow_shared=self.include_shared) == "skip":
                continue
            occupied = plot.occupied_ids
            if len(set(occupied)) != len(occupied) or any(key not in by_id for key in occupied):
                raise FarmError("作物关联地块不完整或重复，已停止操作。")
            if any(by_id[key].master_id != plot.id for key in plot.linked_ids):
                raise FarmError("主地块与关联地块不一致，已停止操作。")
            if len(selected) + len(occupied) > self.settings.max_lands:
                continue
            selected.update({key: by_id[key] for key in occupied})
        return selected

    def planting_groups(self, rows, seed_id=None, *, size_override=None):
        meta = catalog.plant(seed_id or self.settings.seed_id)
        size = size_override or self.seed_layouts.get(
            seed_id or self.settings.seed_id, meta.get("plot_size", 1)
        )
        empty = {
            plot.id: plot
            for plot in rows.values()
            if plot.status(self.client.server_now(), allow_shared=self.include_shared) == "empty"
        }
        if size == 1:
            return [[key] for key in empty]
        if size != 2:
            raise FarmError("当前植物的占地尺寸尚未核对。")
        groups = []
        used = set()
        # 当前 24 块地为每行 4 格；已核对请求顺序为下左、下右、上左、上右。
        for lower_left in (5, 7, 13, 15, 21, 23):
            ids = [lower_left, lower_left + 1, lower_left - 4, lower_left - 3]
            if not set(ids).issubset(empty) or used.intersection(ids):
                continue
            supported = meta.get("supported_land_levels", [])
            if supported and any(empty[key].level not in supported for key in ids):
                continue
            groups.append(ids)
            used.update(ids)
        return groups

    def next_delay(self, rows):
        now = self.client.server_now()
        active = [
            land
            for land in rows.values()
            if land.status(now, allow_shared=self.include_shared) in {"growing", "mature"}
        ]
        # A round can spend minutes harvesting and planting other lands. Crops that
        # became due during that work must run before the newly planted crop timers.
        if not self.grouped_maturity and any(
            (land.mature_at and land.mature_at + self.settings.maturity_grace_seconds + 2 <= now)
            or (
                not land.mature_at
                and land.status(now, allow_shared=self.include_shared) == "mature"
            )
            for land in active
        ):
            return 5
        targets = [
            land.mature_at + self.settings.maturity_grace_seconds + 2
            for land in active
            if land.mature_at and land.mature_at + self.settings.maturity_grace_seconds + 2 > now
        ]
        if targets and (self.settings.maturity_grace_seconds or self.settings.random_action_delays):
            return max(5, (max(targets) if self.grouped_maturity else min(targets)) - now)
        return (
            max(
                5,
                min(
                    self.settings.poll_max,
                    (max(targets) if self.grouped_maturity else min(targets)) - now,
                ),
            )
            if targets
            else self.settings.poll_max
        )

    @staticmethod
    def updated(rows, payload, expected_id):
        updates = models.lands(payload)
        target = next((land for land in updates if land.id == expected_id), None)
        if target is None:
            raise FarmError("操作回复未包含目标土地状态，已停止，避免重复操作。")
        for land in updates:
            if land.id in rows:
                rows[land.id] = land
        return target

    @staticmethod
    def updated_batch(rows, payload, expected_ids):
        updates = models.lands(payload)
        by_id = {land.id: land for land in updates}
        if len(by_id) != len(updates) or not set(expected_ids).issubset(by_id):
            raise FarmError("批量回复的土地记录不完整或重复，已停止，避免重复操作。")
        for land_id, land in by_id.items():
            if land_id in rows:
                rows[land_id] = land
        return by_id

    async def round(self, *, initial_rows=None):
        rows = await self.get_lands() if initial_rows is None else initial_rows
        self.latest_rows = rows
        now = self.client.server_now()
        plan = [
            {
                "land": land.id,
                "status": land.status(now, allow_shared=self.include_shared),
                "plant": land.plant_name,
                "mature_at": land.mature_at,
                "master_id": land.master_id,
                "plot_size": land.plot_size,
                "root_name": rows[land.master_id].plant_name
                if land.master_id in rows
                else land.plant_name,
                "root_mature_at": rows[land.master_id].mature_at
                if land.master_id in rows
                else land.mature_at,
            }
            for land in rows.values()
        ]
        self.audit.record("round", "planned", apply=self.apply, plots=plan)
        if not self.apply:
            return rows
        mature = [
            land.id
            for land in rows.values()
            if land.status(self.client.server_now(), allow_shared=self.include_shared) == "mature"
            and (
                not land.mature_at
                or self.client.server_now() >= land.mature_at + self.settings.maturity_grace_seconds
            )
        ]
        if mature:
            body = (
                wire.packed(1, mature)
                + wire.uint(2, self.client.gid)
                + wire.uint(3, 1)
                + wire.uint(4, 0)
                + wire.uint(5, 0)
            )
            result = await self.client.rpc(PLANT, "Harvest", body)
            try:
                self.updated_batch(rows, result, mature)
            except (FarmError, wire.ProtocolError):
                # A successful write may omit some land states. Check once without
                # resending Harvest, and never attribute a later change to this client.
                self.audit.record("harvest_verification", "started", plots=mature)
                try:
                    latest_payload = await self.client.rpc(PLANT, "AllLands")
                    latest_list = models.lands(latest_payload)
                    latest = {land.id: land for land in latest_list}
                except Exception as exc:
                    raise UncertainOutcome(
                        "收获回包不完整且土地复查失败；已停止，不重复收获。"
                    ) from exc
                if len(latest) != len(latest_list) or not set(mature).issubset(latest):
                    raise UncertainOutcome(
                        "收获回包不完整且复查土地缺失或重复；已停止，不重复收获。"
                    ) from None
                before = {land_id: rows[land_id] for land_id in mature}
                rows.update({key: land for key, land in latest.items() if key in rows})
                self.land_payload = latest_payload
                unresolved = [
                    land_id
                    for land_id in mature
                    if latest[land_id].plant_id == before[land_id].plant_id
                    and latest[land_id].status(
                        self.client.server_now(), allow_shared=self.include_shared
                    ) in {"mature", "unknown"}
                ]
                if unresolved:
                    self.audit.record(
                        "harvest_verification", "unknown", plots=unresolved
                    )
                    raise UncertainOutcome(
                        "收获复查仍有目标未能确认；已停止，不重复收获。"
                    ) from None
                self.audit.record(
                    "harvest_verification", "received", plots=mature,
                    reason="复查确认目标已变化；不重复收获，也不计入本助手确认次数。",
                )
            else:
                self.audit.record(
                    "harvest_batch",
                    "confirmed",
                    plots=mature,
                    items=[models.item(value) for value in wire.parse(result).get(2, [])],
                )
                for land_id in mature:
                    self.audit.record(
                        "harvest",
                        "confirmed",
                        land=land_id,
                        next_status=rows[land_id].status(
                            self.client.server_now(), allow_shared=self.include_shared
                        ),
                    )
        # 仅清理回包确认的残留；多季作物继续生长时保留。
        residue = [
            land.id
            for land in rows.values()
            if land.status(self.client.server_now(), allow_shared=self.include_shared) == "residue"
        ]
        if residue:
            result = await self.client.rpc(PLANT, "RemovePlant", wire.packed(1, residue))
            updates = self.updated_batch(rows, result, residue)
            if any(
                updates[land_id].status(self.client.server_now(), allow_shared=self.include_shared)
                != "empty"
                for land_id in residue
            ):
                raise FarmError("清理回复没有确认全部目标空地，已停止播种。")
            for land_id in residue:
                self.audit.record("cleanup", "confirmed", land=land_id)
        if self.seed_priority is not None:
            await self.plant_priority(rows)
            return rows
        groups = self.planting_groups(rows)
        if not groups:
            return rows
        if self.settings.random_plant_order:
            random.shuffle(groups)
        inventory = models.bag(await self.client.rpc(ITEM, "Bag"))
        available = inventory.get(self.settings.seed_id, 0)
        needed = len(groups) - available
        if needed > 0 and not catalog.plant(self.settings.seed_id).get("inventory_only"):
            goods = [
                good
                for good in models.shop(await self.client.rpc(SHOP, "ShopInfo", wire.uint(1, 2)))
                if good.item_id == self.settings.seed_id
                and good.unlocked
                and 0 < good.price <= self.settings.purchase_budget
                and good.item_count > 0
            ]
            if goods:
                good = min(goods, key=lambda value: (value.price / value.item_count, value.id))
                purchases = math.ceil(needed / good.item_count)
                purchases = min(
                    purchases,
                    self.settings.purchase_budget // good.price,
                    self.client.gold // good.price,
                )
                if good.limit:
                    purchases = min(purchases, max(0, good.limit - good.bought))
                if purchases > 0:
                    body = (
                        wire.uint(1, good.id) + wire.uint(2, purchases) + wire.uint(3, good.price)
                    )
                    response = wire.parse(await self.client.rpc(SHOP, "BuyGoods", body))
                    obtained = [models.item(value) for value in response.get(2, [])]
                    actual = sum(
                        count for item_id, count in obtained if item_id == self.settings.seed_id
                    )
                    if not actual:
                        raise FarmError("购买回复未确认获得指定种子，已停止，不自动重买。")
                    available += actual
                    self.audit.record(
                        "purchase",
                        "confirmed",
                        goods=good.id,
                        seed=self.settings.seed_id,
                        obtained=actual,
                        price=good.price,
                        purchases=purchases,
                    )
                    # 读取实际余额，避免假设回包顺序或重复扣减推送中的金币。
                    for value in response.get(3, []):
                        item_id, count = models.item(value)
                        if item_id == 1001:
                            self.audit.record("purchase_cost", "reported", coins=count)
            else:
                self.audit.record("purchase", "skipped", reason="no_eligible_goods_or_budget")
        for ids in groups[:available]:
            item = wire.uint(1, self.settings.seed_id) + wire.packed(2, ids)
            response = await self.client.rpc(PLANT, "Plant", wire.blob(2, item))
            if len(ids) == 1:
                grown = self.updated(rows, response, ids[0])
            else:
                updates = self.updated_batch(rows, response, ids)
                grown = updates[ids[0]]
                if set(grown.occupied_ids) != set(ids) or any(
                    updates[key].master_id != grown.id for key in ids[1:]
                ):
                    raise FarmError("四格播种回包没有确认完整占地，已停止，不能重复播种。")
            if not grown.plant_id:
                raise FarmError("播种回复未确认植物存在，已停止，不自动补种同一地块。")
            self.audit.record(
                "plant",
                "confirmed",
                land=ids[0],
                occupied_lands=ids,
                seed=self.settings.seed_id,
                mature_at=grown.mature_at,
            )
        return rows

    async def plant_priority(self, rows):
        """Each priority gets the remaining eligible space; confirmed purchases only."""
        priorities = list(self.seed_priority)
        if self.fallback_radish and 20002 not in priorities:
            priorities.append(20002)
        if not priorities:
            return
        if not any(
            plot.status(self.client.server_now(), allow_shared=self.include_shared) == "empty"
            for plot in rows.values()
        ):
            return
        self.bag_payload = await self.client.rpc(ITEM, "Bag")
        self.inventory = models.bag(self.bag_payload)
        initial_gold = self.client.gold
        spent = 0
        goods = None
        for seed_id in priorities:
            if self.checkpoint:
                await self.checkpoint(rows)
            meta = catalog.plant(seed_id)
            if self.seed_layouts.get(seed_id, meta.get("plot_size", 1)) not in {1, 2}:
                self.decisions.append({"seed": seed_id, "reason": "占地尺寸尚未核对"})
                continue
            groups = self.planting_groups(rows, seed_id)
            if not groups:
                self.decisions.append({"seed": seed_id, "reason": "没有符合布局和等级的空地"})
                continue
            if self.settings.random_plant_order:
                random.shuffle(groups)
            available = self.inventory.get(seed_id, 0)
            needed = max(0, len(groups) - available)
            funds = max(0, min(self.client.gold, initial_gold - spent))
            if self.purchase_mode == "budget":
                funds = min(funds, max(0, self.settings.purchase_budget - spent))
            elif self.purchase_mode == "inventory":
                funds = 0
            if needed and funds and not meta.get("inventory_only"):
                if goods is None:
                    self.shop_payload = await self.client.rpc(SHOP, "ShopInfo", wire.uint(1, 2))
                    goods = models.shop(self.shop_payload)
                eligible = [
                    good
                    for good in goods
                    if good.item_id == seed_id
                    and good.unlocked
                    and 0 < good.price <= funds
                    and good.item_count > 0
                    and (not good.limit or good.bought < good.limit)
                ]
                if eligible:
                    good = min(eligible, key=lambda item: (item.price / item.item_count, item.id))
                    count = min(math.ceil(needed / good.item_count), funds // good.price)
                    if good.limit:
                        count = min(count, max(0, good.limit - good.bought))
                    body = wire.uint(1, good.id) + wire.uint(2, count) + wire.uint(3, good.price)
                    response = wire.parse(await self.client.rpc(SHOP, "BuyGoods", body))
                    obtained = sum(
                        amount
                        for item_id, amount in (models.item(value) for value in response.get(2, []))
                        if item_id == seed_id
                    )
                    if not obtained:
                        raise FarmError("购买回包未确认指定种子，已暂停，不自动重买。")
                    available += obtained
                    self.inventory[seed_id] = available
                    spent += good.price * count
                    self.audit.record(
                        "purchase",
                        "confirmed",
                        goods=good.id,
                        seed=seed_id,
                        obtained=obtained,
                        price=good.price,
                        purchases=count,
                    )
            if not available:
                self.decisions.append(
                    {
                        "seed": seed_id,
                        "reason": "库存为零且不可购买（金币、解锁、限购或购买模式）",
                    }
                )
                continue
            planted = 0
            while self.inventory.get(seed_id, 0) > 0:
                if self.checkpoint:
                    await self.checkpoint(rows)
                # Recompute after every reply or manual action; a newly merged crop
                # can occupy other cells from the old list of empty plots.
                groups = self.planting_groups(rows, seed_id)
                if meta.get("layout_inferred") and seed_id not in self.seed_layouts:
                    probes = self.planting_groups(rows, seed_id, size_override=2)
                    if not probes:
                        self.decisions.append(
                            {"seed": seed_id, "reason": "空地不足以确认活动种子布局"}
                        )
                        break
                    group = random.choice(probes) if self.settings.random_plant_order else probes[0]
                    ids = [group[0]]
                else:
                    if not groups:
                        break
                    ids = random.choice(groups) if self.settings.random_plant_order else groups[0]
                item = wire.uint(1, seed_id) + wire.packed(2, ids)
                response = await self.client.rpc(PLANT, "Plant", wire.blob(2, item))
                updates = self.updated_batch(rows, response, ids)
                grown = updates[ids[0]]
                occupied = list(grown.occupied_ids)
                if (
                    not grown.plant_id
                    or grown.plot_size not in {1, 2}
                    or len(set(occupied)) != grown.plot_size**2
                    or not set(occupied).issubset(updates)
                    or not set(occupied).issubset(rows)
                    or any(updates[key].master_id != grown.id for key in grown.linked_ids)
                    or (
                        len(ids) > 1
                        and (
                            set(grown.occupied_ids) != set(ids)
                            or any(updates[key].master_id != grown.id for key in ids[1:])
                        )
                    )
                ):
                    raise FarmError("播种回包未确认完整作物，已暂停，不重复播种。")
                self.seed_layouts[seed_id] = grown.plot_size
                self.inventory[seed_id] = max(0, self.inventory.get(seed_id, 0) - 1)
                planted += 1
                self.audit.record(
                    "plant",
                    "confirmed",
                    land=ids[0],
                    occupied_lands=occupied,
                    plot_size=grown.plot_size,
                    seed=seed_id,
                    mature_at=grown.mature_at,
                )
            self.decisions.append(
                {
                    "seed": seed_id,
                    "planted": planted,
                    "reason": "已播种" if planted else "当前空地不适用",
                }
            )
            if not any(
                plot.status(self.client.server_now(), allow_shared=self.include_shared) == "empty"
                for plot in rows.values()
            ):
                break

    async def run(self, *, once=False):
        while True:
            rows = await self.round()
            if once or not self.apply:
                return rows
            delay = self.next_delay(rows)
            self.audit.record("schedule", "waiting", seconds=round(delay, 2))
            await self.client.wait(delay)
