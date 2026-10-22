"""SQLite 缺少的型別：帶時區的時間、精確的 Decimal。"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from chargeshare.clock import TAIPEI


class TZDateTime(TypeDecorator[datetime]):
    """以 UTC 寫入，讀出時轉為台灣時間的 aware datetime；拒絕 naive datetime。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"datetime must be timezone-aware, got naive {value!r}")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC).astimezone(TAIPEI)


class DecimalText(TypeDecorator[Decimal]):
    """以字串儲存 Decimal，避免 SQLite 轉成浮點數。"""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)
