from datetime import date
from decimal import Decimal

import pytest

from chargeshare.domain.tariff import Band, HolidayDataMissing, RatePeriod, Season
from tests.conftest import tw

SUMMER_PEAK = RatePeriod(Season.SUMMER, Band.PEAK)
SUMMER_OFF = RatePeriod(Season.SUMMER, Band.OFFPEAK)
NON_PEAK = RatePeriod(Season.NON_SUMMER, Band.PEAK)
NON_OFF = RatePeriod(Season.NON_SUMMER, Band.OFFPEAK)


def test_config_loads_rates_and_holidays(tariff):
    assert tariff.rate(SUMMER_PEAK) == Decimal("5.0")
    assert tariff.rate(NON_OFF) == Decimal("1.9")
    assert tariff.management_fee_per_kwh == Decimal("0.5")
    assert date(2026, 10, 9) in tariff.holidays


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (tw(2026, 7, 1, 8, 59), SUMMER_OFF),  # 週三，夏月尖峰前一分鐘
        (tw(2026, 7, 1, 9, 0), SUMMER_PEAK),
        (tw(2026, 7, 1, 23, 59), SUMMER_PEAK),
        (tw(2026, 7, 2, 0, 0), SUMMER_OFF),  # 24:00 後回到離峰
        (tw(2026, 10, 22, 6, 0), NON_PEAK),  # 週四
        (tw(2026, 10, 22, 11, 0), NON_OFF),  # 非夏月午間離峰
        (tw(2026, 10, 22, 13, 59), NON_OFF),
        (tw(2026, 10, 22, 14, 0), NON_PEAK),
        (tw(2026, 10, 24, 12, 0), NON_OFF),  # 週六
        (tw(2026, 10, 25, 20, 0), NON_OFF),  # 週日
    ],
)
def test_period_at_weekday_and_weekend(tariff, when, expected):
    assert tariff.period_at(when) == expected


def test_holiday_on_weekday_is_offpeak_all_day(tariff):
    # 2026-10-09 週五，國慶日補假
    assert tariff.period_at(tw(2026, 10, 9, 20, 0)) == NON_OFF


def test_season_boundaries_are_inclusive(tariff):
    assert tariff.period_at(tw(2026, 5, 31, 23, 59)).season == Season.NON_SUMMER
    assert tariff.period_at(tw(2026, 6, 1, 0, 0)).season == Season.SUMMER
    assert tariff.period_at(tw(2026, 9, 30, 23, 59)).season == Season.SUMMER
    assert tariff.period_at(tw(2026, 10, 1, 0, 0)).season == Season.NON_SUMMER


def test_period_at_normalizes_other_timezones(tariff):
    from datetime import UTC, datetime

    # UTC 01:00 = 台灣 09:00（夏月平日尖峰開始）
    assert tariff.period_at(datetime(2026, 7, 1, 1, 0, tzinfo=UTC)) == SUMMER_PEAK


def test_next_change_returns_next_boundary_or_midnight(tariff):
    assert tariff.next_change(tw(2026, 10, 22, 7, 30)) == tw(2026, 10, 22, 11, 0)
    assert tariff.next_change(tw(2026, 10, 22, 11, 0)) == tw(2026, 10, 22, 14, 0)
    assert tariff.next_change(tw(2026, 10, 22, 15, 0)) == tw(2026, 10, 23, 0, 0)


def test_year_without_holiday_data_fails_fast(tariff):
    with pytest.raises(HolidayDataMissing, match="2027"):
        tariff.period_at(tw(2027, 1, 4, 10, 0))
