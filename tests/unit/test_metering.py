from datetime import datetime, timedelta

import pytest

from chargeshare.domain.metering import Reading, split_energy
from chargeshare.domain.tariff import Band, RatePeriod, Season
from tests.unit.conftest import tw


def constant_power(start: datetime, end: datetime, power_w: int, meter_start: int = 0):
    """每分鐘一筆讀數，模擬定功率充電的累計電表。"""
    readings = []
    t = start
    while t <= end:
        elapsed = (t - start).total_seconds()
        readings.append(Reading(t, meter_start + round(power_w * elapsed / 3600)))
        t += timedelta(minutes=1)
    return readings


def test_friday_night_to_saturday_morning(tariff):
    # 驗收 6：週五 23:00 → 週六 07:00，7 kW 定速（非夏月）
    readings = constant_power(tw(2026, 10, 23, 23, 0), tw(2026, 10, 24, 7, 0), 7000)
    split = split_energy(readings, tariff)
    assert split == {
        RatePeriod(Season.NON_SUMMER, Band.PEAK): 7000,  # 週五 23–24
        RatePeriod(Season.NON_SUMMER, Band.OFFPEAK): 49000,  # 週六 00–07
    }


def test_crossing_into_summer_switches_rate_at_midnight(tariff):
    # 驗收 7：5/31（日）22:00 → 6/1（一）08:00，全為離峰但季節不同
    readings = constant_power(tw(2026, 5, 31, 22, 0), tw(2026, 6, 1, 8, 0), 7000)
    split = split_energy(readings, tariff)
    assert split == {
        RatePeriod(Season.NON_SUMMER, Band.OFFPEAK): 14000,
        RatePeriod(Season.SUMMER, Band.OFFPEAK): 56000,
    }


def test_gap_across_boundary_is_split_linearly_by_time(tariff):
    # 斷線造成 08:30 → 09:30 只有頭尾兩筆，夏月平日 09:00 切換尖峰
    readings = [Reading(tw(2026, 7, 1, 8, 30), 1000), Reading(tw(2026, 7, 1, 9, 30), 8000)]
    split = split_energy(readings, tariff)
    assert split == {
        RatePeriod(Season.SUMMER, Band.OFFPEAK): 3500,
        RatePeriod(Season.SUMMER, Band.PEAK): 3500,
    }


def test_rounding_keeps_total_equal_to_meter_delta(tariff):
    # 3 Wh 平分到兩個時段，各 1.5 Wh；個別四捨五入會變成 4 Wh
    readings = [Reading(tw(2026, 7, 1, 8, 59, 30), 0), Reading(tw(2026, 7, 1, 9, 0, 30), 3)]
    split = split_energy(readings, tariff)
    assert sum(split.values()) == 3


def test_unsorted_readings_are_sorted_by_time(tariff):
    readings = constant_power(tw(2026, 10, 22, 10, 0), tw(2026, 10, 22, 12, 0), 3600)
    split = split_energy(list(reversed(readings)), tariff)
    assert sum(split.values()) == 7200


def test_single_reading_yields_no_energy(tariff):
    assert split_energy([Reading(tw(2026, 10, 22, 10, 0), 500)], tariff) == {}


def test_decreasing_meter_fails_fast(tariff):
    readings = [Reading(tw(2026, 10, 22, 10, 0), 5000), Reading(tw(2026, 10, 22, 10, 1), 4000)]
    with pytest.raises(ValueError, match="decreased"):
        split_energy(readings, tariff)


def test_same_timestamp_with_different_energy_fails_fast(tariff):
    readings = [Reading(tw(2026, 10, 22, 10, 0), 5000), Reading(tw(2026, 10, 22, 10, 0), 5100)]
    with pytest.raises(ValueError, match="same timestamp"):
        split_energy(readings, tariff)
