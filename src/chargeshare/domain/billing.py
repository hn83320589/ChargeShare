"""充電金額與帳單總額計算。"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from chargeshare.domain.tariff import Band, RatePeriod, Tariff

WH_PER_KWH = Decimal(1000)


@dataclass(frozen=True)
class Charge:
    peak_wh: int
    offpeak_wh: int
    energy_amount: Decimal  # 電費，未捨入
    fee_amount: Decimal  # 管理費，未捨入

    @property
    def total_wh(self) -> int:
        return self.peak_wh + self.offpeak_wh

    @property
    def amount(self) -> Decimal:
        return self.energy_amount + self.fee_amount


def price(split: Mapping[RatePeriod, int], tariff: Tariff) -> Charge:
    """依各時段 Wh 計算一筆充電的金額（保留精確值，捨入留到帳單層）。"""
    energy_amount = sum(
        (Decimal(wh) / WH_PER_KWH * tariff.rate(period) for period, wh in split.items()),
        Decimal(0),
    )
    peak_wh = sum(wh for period, wh in split.items() if period.band == Band.PEAK)
    offpeak_wh = sum(wh for period, wh in split.items() if period.band == Band.OFFPEAK)
    fee_amount = Decimal(peak_wh + offpeak_wh) / WH_PER_KWH * tariff.management_fee_per_kwh
    return Charge(peak_wh, offpeak_wh, energy_amount, fee_amount)


def bill_total(amounts: Iterable[Decimal]) -> int:
    """帳單總額：各筆精確金額加總後，四捨五入到元。"""
    total = sum(amounts, Decimal(0))
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))
