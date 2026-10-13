from dataclasses import dataclass

from qqfarm import wire


@dataclass(frozen=True)
class Phase:
    phase: int
    phase_id: int
    begin_time: int


@dataclass(frozen=True)
class Land:
    id: int
    unlocked: bool
    shared: bool
    master_id: int
    plant_id: int
    plant_name: str
    phases: tuple[Phase, ...]
    dry_num: int = 0
    weed_owners: tuple[int, ...] = ()
    insect_owners: tuple[int, ...] = ()
    stealable: bool = False
    fruit_id: int = 0
    left_fruit: int = 0
    normal_fertilizer_left: int = 0
    linked_ids: tuple[int, ...] = ()
    plot_size: int = 1
    level: int = 0
    merge_type: int = 0

    def status(self, now: float, *, allow_shared=False):
        if not self.unlocked or (self.shared and not allow_shared) or self.master_id:
            return "skip"
        if not self.plant_id:
            return "empty"
        current = [p for p in self.phases if p.begin_time <= now]
        if not current:
            return "unknown"
        phase = max(current, key=lambda p: p.begin_time).phase
        return "mature" if phase == 6 else "residue" if phase == 7 else "growing"

    @property
    def mature_at(self):
        values = [p.begin_time for p in self.phases if p.phase == 6 and p.begin_time > 0]
        return min(values) if values else None

    @property
    def occupied_ids(self):
        return (self.id, *self.linked_ids)


def land(value: bytes):
    f = wire.parse(value)
    plant = wire.parse(wire.data(f, 10))
    linked = tuple(repeated_numbers(f.get(14, [])))
    phases = tuple(
        Phase(wire.number(p, 1), wire.number(p, 3), wire.number(p, 2))
        for p in (wire.parse(x) for x in plant.get(4, []))
    )
    return Land(
        wire.number(f, 1),
        bool(wire.number(f, 2)),
        bool(wire.number(f, 11)),
        wire.number(f, 13),
        wire.number(plant, 1),
        wire.string(plant, 2),
        phases,
        wire.number(plant, 6),
        tuple(repeated_numbers(plant.get(12, []))),
        tuple(repeated_numbers(plant.get(13, []))),
        bool(wire.number(plant, 16)),
        wire.number(plant, 10),
        wire.number(plant, 18),
        wire.number(plant, 17),
        linked,
        2 if len(linked) == 3 else 1,
        wire.number(f, 3),
        wire.number(f, 15),
    )


def lands(payload):
    return [land(value) for value in wire.parse(payload).get(1, [])]


def repeated_numbers(values):
    result = []
    for value in values:
        if isinstance(value, int):
            result.append(value)
        else:
            offset = 0
            while offset < len(value):
                number, offset = wire.read_varint(value, offset)
                result.append(number)
    return result


def item(value):
    f = wire.parse(value)
    return wire.number(f, 1), wire.number(f, 2)


def bag(payload):
    f = wire.parse(wire.data(wire.parse(payload), 1))
    result = {}
    for value in f.get(1, []):
        key, count = item(value)
        result[key] = result.get(key, 0) + count
    return result


@dataclass(frozen=True)
class Goods:
    id: int
    price: int
    limit: int
    bought: int
    unlocked: bool
    item_id: int
    item_count: int


def shop(payload):
    result = []
    for value in wire.parse(payload).get(1, []):
        f = wire.parse(value)
        result.append(
            Goods(
                wire.number(f, 1),
                wire.number(f, 3),
                wire.number(f, 4),
                wire.number(f, 2),
                bool(wire.number(f, 5)),
                wire.number(f, 6),
                wire.number(f, 7),
            )
        )
    return result
