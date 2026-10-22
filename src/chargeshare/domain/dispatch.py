"""把新的分配結果轉成下達計畫：先降後升，並以死區避免小幅反覆調升。"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class DispatchPlan:
    decreases: dict[str, int]  # 第一階段：立即下達
    increases: dict[str, int]  # 第二階段：調降生效後才下達

    @property
    def is_empty(self) -> bool:
        return not self.decreases and not self.increases


def plan_dispatch(
    current: Mapping[str, int], target: Mapping[str, int], deadband_w: int
) -> DispatchPlan:
    decreases: dict[str, int] = {}
    increases: dict[str, int] = {}
    for charger_id in current.keys() | target.keys():
        old = current.get(charger_id, 0)
        new = target.get(charger_id, 0)
        if new < old:
            # 調降一律下達：保留舊值可能讓總和超過容量
            decreases[charger_id] = new
        elif new - old >= deadband_w:
            increases[charger_id] = new
    return DispatchPlan(decreases, increases)
