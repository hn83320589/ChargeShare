"""二段式時間電價：判定某時刻屬於哪個季節與尖離峰時段。"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from chargeshare.clock import TAIPEI

MINUTES_PER_DAY = 24 * 60


class Season(StrEnum):
    SUMMER = "summer"
    NON_SUMMER = "non_summer"


class Band(StrEnum):
    PEAK = "peak"
    OFFPEAK = "offpeak"


@dataclass(frozen=True)
class RatePeriod:
    season: Season
    band: Band


class HolidayDataMissing(LookupError):
    pass


@dataclass(frozen=True)
class SeasonRule:
    peak_rate: Decimal
    offpeak_rate: Decimal
    # 平日尖峰區間，以「當日第幾分鐘」表示的 [start, end)
    weekday_peak_windows: tuple[tuple[int, int], ...]

    def is_peak_minute(self, minute_of_day: int) -> bool:
        return any(start <= minute_of_day < end for start, end in self.weekday_peak_windows)

    def boundaries(self) -> list[int]:
        edges = {m for window in self.weekday_peak_windows for m in window}
        edges.add(MINUTES_PER_DAY)
        return sorted(m for m in edges if 0 < m <= MINUTES_PER_DAY)


@dataclass(frozen=True)
class Tariff:
    summer: SeasonRule
    non_summer: SeasonRule
    summer_start: tuple[int, int]  # (月, 日)，含
    summer_end: tuple[int, int]  # (月, 日)，含
    management_fee_per_kwh: Decimal
    holidays: frozenset[date]
    holiday_years: frozenset[int]

    def season_on(self, day: date) -> Season:
        if self.summer_start <= (day.month, day.day) <= self.summer_end:
            return Season.SUMMER
        return Season.NON_SUMMER

    def rule(self, season: Season) -> SeasonRule:
        return self.summer if season == Season.SUMMER else self.non_summer

    def rate(self, period: RatePeriod) -> Decimal:
        rule = self.rule(period.season)
        return rule.peak_rate if period.band == Band.PEAK else rule.offpeak_rate

    def is_offpeak_day(self, day: date) -> bool:
        if day.year not in self.holiday_years:
            raise HolidayDataMissing(
                f"no holiday data for year {day.year}; add config/holidays_{day.year}.toml"
            )
        return day.weekday() >= 5 or day in self.holidays

    def period_at(self, when: datetime) -> RatePeriod:
        local = when.astimezone(TAIPEI)
        season = self.season_on(local.date())
        if self.is_offpeak_day(local.date()):
            return RatePeriod(season, Band.OFFPEAK)
        minute = local.hour * 60 + local.minute
        band = Band.PEAK if self.rule(season).is_peak_minute(minute) else Band.OFFPEAK
        return RatePeriod(season, band)

    def next_change(self, when: datetime) -> datetime:
        """回傳 when 之後、時段「可能」改變的最近時間點（當日尖離峰邊界或午夜）。"""
        local = when.astimezone(TAIPEI)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = local - midnight
        for boundary in self.rule(self.season_on(local.date())).boundaries():
            if timedelta(minutes=boundary) > elapsed:
                return midnight + timedelta(minutes=boundary)
        raise AssertionError("boundaries always include midnight")  # pragma: no cover
