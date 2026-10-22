"""動態功率分配：在總容量內，依先來後到與各樁需求分配功率上限。"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class PowerConfig:
    capacity_w: int = 30_000
    min_w: int = 1_400  # 6A，低於此值車輛無法充電
    max_w: int = 7_000
    step_w: int = 50  # 約 0.2A；3.75 kW、1.4 kW 皆可整除


@dataclass(frozen=True)
class ChargerInput:
    charger_id: str
    online: bool
    has_session: bool  # 有進行中的交易
    arrived_at: datetime  # 排隊順序依據
    demand_cap_w: int  # 需求上限，見 domain.demand
    held_limit_w: int  # 樁目前可能正在使用的上限（已生效或已送出未確認，取較大者）


@dataclass(frozen=True)
class Allocation:
    limits: dict[str, int]  # 線上且有交易的樁 → 上限（排隊中為 0）
    queued: tuple[str, ...]  # 排隊中的樁，依先來後到
    reserved: dict[str, int] = field(default_factory=dict)  # 離線樁保留的額度

    @property
    def total_w(self) -> int:
        return sum(self.limits.values()) + sum(self.reserved.values())


def allocate(chargers: Iterable[ChargerInput], config: PowerConfig) -> Allocation:
    chargers = list(chargers)
    # 1. 離線但有交易的樁可能仍依最後上限供電，額度必須保留
    reserved = {
        c.charger_id: c.held_limit_w
        for c in chargers
        if not c.online and c.has_session and c.held_limit_w > 0
    }
    budget = max(config.capacity_w - sum(reserved.values()), 0)

    # 2. 依先來後到，最多讓 budget / min_w 支樁供電，其餘排隊
    active = sorted(
        (c for c in chargers if c.online and c.has_session),
        key=lambda c: (c.arrived_at, c.charger_id),
    )
    slots = budget // config.min_w
    served, waiting = active[:slots], active[slots:]

    # 3. 注水法分配
    limits = _water_fill(served, budget, config)
    limits.update({c.charger_id: 0 for c in waiting})
    return Allocation(limits, tuple(c.charger_id for c in waiting), reserved)


def _water_fill(served: list[ChargerInput], budget: int, config: PowerConfig) -> dict[str, int]:
    """需求低的樁只拿需求量，省下的額度由其餘樁平分；結果向下取整到 step_w。"""
    caps = sorted(
        ((min(max(c.demand_cap_w, config.min_w), config.max_w), c.charger_id) for c in served),
    )
    limits: dict[str, int] = {}
    remaining = budget
    for index, (cap, charger_id) in enumerate(caps):
        level = remaining // (len(caps) - index)
        if cap > level:
            # 其餘樁的需求都高於水位：全部拿同一個水位，捨去的零頭不再分配
            shared = _floor_to_step(level, config.step_w)
            limits.update((cid, shared) for _, cid in caps[index:])
            break
        limits[charger_id] = _floor_to_step(cap, config.step_w)
        remaining -= limits[charger_id]
    return limits


def _floor_to_step(value: int, step: int) -> int:
    return value // step * step
