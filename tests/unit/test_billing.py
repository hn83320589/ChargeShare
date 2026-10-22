from decimal import Decimal

from chargeshare.domain.billing import bill_total, price
from chargeshare.domain.tariff import Band, RatePeriod, Season

NON_PEAK = RatePeriod(Season.NON_SUMMER, Band.PEAK)
NON_OFF = RatePeriod(Season.NON_SUMMER, Band.OFFPEAK)
SUMMER_OFF = RatePeriod(Season.SUMMER, Band.OFFPEAK)


def test_price_friday_night_session(tariff):
    # 7 度 × 4.8 + 49 度 × 1.9 + 56 度 × 0.5 = 33.6 + 93.1 + 28 = 154.7
    charge = price({NON_PEAK: 7000, NON_OFF: 49000}, tariff)
    assert charge.peak_wh == 7000
    assert charge.offpeak_wh == 49000
    assert charge.total_wh == 56000
    assert charge.energy_amount == Decimal("126.7")
    assert charge.fee_amount == Decimal("28.0")
    assert charge.amount == Decimal("154.7")


def test_price_across_seasons_uses_each_season_rate(tariff):
    # 14 度 × 1.9 + 56 度 × 2.0 + 70 度 × 0.5 = 26.6 + 112 + 35 = 173.6
    charge = price({NON_OFF: 14000, SUMMER_OFF: 56000}, tariff)
    assert charge.peak_wh == 0
    assert charge.offpeak_wh == 70000
    assert charge.amount == Decimal("173.6")


def test_price_with_fractional_kwh(tariff):
    # 1.234 度尖峰 × 4.8 = 5.9232；管理費 0.617
    charge = price({NON_PEAK: 1234}, tariff)
    assert charge.amount == Decimal("6.5402")


def test_bill_total_rounds_sum_half_up_to_yuan(tariff):
    # 驗收 11：兩筆充電，尖峰與離峰皆有
    # 第 1 筆 154.70；第 2 筆 10/22（四）10:00–12:00 3.5 kW：
    #   3.5 度尖峰 × 4.8 = 16.8、3.5 度離峰 × 1.9 = 6.65、7 度 × 0.5 = 3.5 → 26.95
    first = price({NON_PEAK: 7000, NON_OFF: 49000}, tariff)
    second = price({NON_PEAK: 3500, NON_OFF: 3500}, tariff)
    assert second.amount == Decimal("26.95")
    assert bill_total([first.amount, second.amount]) == 182  # 181.65 → 182


def test_bill_total_half_rounds_up():
    assert bill_total([Decimal("10.25"), Decimal("0.25")]) == 11
    assert bill_total([Decimal("10.49")]) == 10
    assert bill_total([]) == 0
