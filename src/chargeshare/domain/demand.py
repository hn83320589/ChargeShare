"""每支樁的需求偵測狀態機，以遲滯（hysteresis）避免功率小幅波動造成反覆調整。

HUNGRY ──(實際功率 < 上限 × saturate_ratio，連續 streak 筆)──▶ SATURATED(cap)
SATURATED ──(實際功率 ≥ min(cap, 上限) × recover_ratio)──▶ HUNGRY
SATURATED 時 cap 只會往下調，且必須低於目前 cap 一整個級距才調整。
"""

from dataclasses import dataclass
from enum import StrEnum


class DemandMode(StrEnum):
    HUNGRY = "hungry"
    SATURATED = "saturated"


@dataclass(frozen=True)
class DemandConfig:
    saturate_ratio: float = 0.80
    recover_ratio: float = 0.95
    streak: int = 2
    headroom_w: int = 500
    cap_step_w: int = 500
    max_w: int = 7_000


@dataclass(frozen=True)
class DemandState:
    mode: DemandMode
    cap_w: int
    low_streak: int = 0

    @classmethod
    def initial(cls, config: DemandConfig) -> "DemandState":
        return cls(DemandMode.HUNGRY, config.max_w)


def update_demand(
    state: DemandState, power_w: int, limit_w: int, config: DemandConfig
) -> DemandState:
    if limit_w <= 0:
        # 排隊中或尚未下達上限，量到的功率不代表車的需求
        return state

    if state.mode == DemandMode.HUNGRY:
        if power_w >= limit_w * config.saturate_ratio:
            return DemandState.initial(config)
        streak = state.low_streak + 1
        if streak < config.streak:
            return DemandState(DemandMode.HUNGRY, config.max_w, streak)
        return DemandState(DemandMode.SATURATED, _needed_cap(power_w, config))

    if power_w >= min(state.cap_w, limit_w) * config.recover_ratio:
        return DemandState.initial(config)
    needed = _needed_cap(power_w, config)
    if needed <= state.cap_w - config.cap_step_w:
        return DemandState(DemandMode.SATURATED, needed)
    return state


def _needed_cap(power_w: int, config: DemandConfig) -> int:
    wanted = power_w + config.headroom_w
    stepped = -(-wanted // config.cap_step_w) * config.cap_step_w  # 向上取整到級距
    return min(stepped, config.max_w)
