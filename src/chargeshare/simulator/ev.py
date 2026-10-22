"""電動車電池模型：SOC 超過 taper_from 後可接受功率線性下降（快充滿降載）。"""

from dataclasses import dataclass

MIN_TAPER_W = 300.0


@dataclass
class Battery:
    capacity_wh: float
    soc: float  # 0.0–1.0
    max_w: float = 7_000
    taper_from: float = 0.8

    @property
    def full(self) -> bool:
        return self.soc >= 0.999

    def acceptance_w(self) -> float:
        if self.full:
            return 0.0
        if self.soc <= self.taper_from:
            return self.max_w
        ratio = (1 - self.soc) / (1 - self.taper_from)
        return max(self.max_w * ratio, MIN_TAPER_W)

    def charge(self, energy_wh: float) -> None:
        self.soc = min(1.0, self.soc + energy_wh / self.capacity_wh)
