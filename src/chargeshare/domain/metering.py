"""把累計電表讀數分攤到各電價時段。"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction

from chargeshare.domain.tariff import RatePeriod, Tariff

MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True)
class Reading:
    ts: datetime
    energy_wh: int  # 電表累計值


def split_energy(readings: Iterable[Reading], tariff: Tariff) -> dict[RatePeriod, int]:
    """依相鄰讀數之間的時間比例，把電量線性分攤到各時段，回傳各時段的 Wh。

    讀數下降視為電表重置（見 segment_energy）。結果以最大餘數法取整數 Wh（= 0.001 度），
    保證加總等於各段電量的總和。
    """
    ordered = sorted(readings, key=lambda r: r.ts)
    exact: dict[RatePeriod, Fraction] = defaultdict(Fraction)
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        _validate_pair(prev, cur)
        if cur.ts == prev.ts:
            continue
        delta_wh = segment_energy(prev, cur)
        span = (cur.ts - prev.ts) // MICROSECOND
        t = prev.ts
        while t < cur.ts:
            nxt = min(tariff.next_change(t), cur.ts)
            exact[tariff.period_at(t)] += Fraction(delta_wh * ((nxt - t) // MICROSECOND), span)
            t = nxt
    return _round_preserving_total(exact)


def segment_energy(prev: Reading, cur: Reading) -> int:
    """相鄰兩筆讀數之間的電量。

    讀數下降代表電表重置（例如韌體重開機後從 0 起算）：重置後的電量就是新讀數本身。
    重置前最後一筆讀數到實際歸零之間的電量無從得知，不計入。
    """
    if cur.energy_wh >= prev.energy_wh:
        return cur.energy_wh - prev.energy_wh
    return cur.energy_wh


def count_meter_resets(readings: Iterable[Reading]) -> int:
    ordered = sorted(readings, key=lambda r: r.ts)
    return sum(
        cur.energy_wh < prev.energy_wh for prev, cur in zip(ordered, ordered[1:], strict=False)
    )


def _validate_pair(prev: Reading, cur: Reading) -> None:
    if cur.ts == prev.ts and cur.energy_wh != prev.energy_wh:
        raise ValueError(
            f"two readings at same timestamp {cur.ts.isoformat()} "
            f"disagree: {prev.energy_wh} Wh vs {cur.energy_wh} Wh"
        )


def _round_preserving_total(exact: dict[RatePeriod, Fraction]) -> dict[RatePeriod, int]:
    floors = {period: int(value) for period, value in exact.items()}
    leftover = int(sum(exact.values())) - sum(floors.values())
    by_remainder = sorted(exact, key=lambda p: exact[p] - floors[p], reverse=True)
    for period in by_remainder[:leftover]:
        floors[period] += 1
    return {period: wh for period, wh in floors.items() if wh > 0}
