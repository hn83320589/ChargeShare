"""從 config/ 目錄的 TOML 設定檔建立 domain 物件。"""

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from chargeshare.domain.tariff import MINUTES_PER_DAY, SeasonRule, Tariff


def load_tariff(tariff_path: Path, *holiday_paths: Path) -> Tariff:
    raw = _read_toml(tariff_path)
    holidays = set()
    years = set()
    for path in holiday_paths:
        data = _read_toml(path)
        years.add(int(data["year"]))
        holidays.update(entry["date"] for entry in data["holidays"])
    return Tariff(
        summer=_season_rule(raw["summer"]),
        non_summer=_season_rule(raw["non_summer"]),
        summer_start=_month_day(raw["summer"]["start"]),
        summer_end=_month_day(raw["summer"]["end"]),
        management_fee_per_kwh=Decimal(raw["management_fee_per_kwh"]),
        holidays=frozenset(holidays),
        holiday_years=frozenset(years),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def _season_rule(raw: dict[str, Any]) -> SeasonRule:
    return SeasonRule(
        peak_rate=Decimal(raw["peak_rate"]),
        offpeak_rate=Decimal(raw["offpeak_rate"]),
        weekday_peak_windows=tuple(
            (_minute_of_day(start), _minute_of_day(end)) for start, end in raw["weekday_peak_hours"]
        ),
    )


def _minute_of_day(hhmm: str) -> int:
    hours, minutes = (int(part) for part in hhmm.split(":"))
    minute = hours * 60 + minutes
    if not 0 <= minute <= MINUTES_PER_DAY:
        raise ValueError(f"invalid time of day {hhmm!r}")
    return minute


def _month_day(mmdd: str) -> tuple[int, int]:
    month, day = (int(part) for part in mmdd.split("-"))
    return month, day
