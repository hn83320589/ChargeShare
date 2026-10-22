"""RFID 卡片授權規則。"""

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from chargeshare.clock import TAIPEI

OVERDUE_MONTHS = 2


class AuthStatus(StrEnum):
    # 對應 OCPP idTagInfo.status
    ACCEPTED = "Accepted"
    BLOCKED = "Blocked"
    INVALID = "Invalid"


@dataclass(frozen=True)
class CardInfo:
    id_tag: str
    household_id: int
    active: bool


@dataclass(frozen=True)
class AuthResult:
    status: AuthStatus
    reason: str = ""


def decide_authorization(
    card: CardInfo | None, unpaid_bill_dates: Iterable[date], now: datetime
) -> AuthResult:
    """unpaid_bill_dates：該戶所有未繳帳單的開立日期。"""
    if card is None:
        return AuthResult(AuthStatus.INVALID, "查無此卡片")
    if not card.active:
        return AuthResult(AuthStatus.BLOCKED, "卡片已停用")
    today = now.astimezone(TAIPEI).date()
    overdue = [d for d in unpaid_bill_dates if today > add_months(d, OVERDUE_MONTHS)]
    if overdue:
        oldest = min(overdue)
        return AuthResult(
            AuthStatus.BLOCKED, f"有超過 {OVERDUE_MONTHS} 個月未繳的帳單（{oldest} 開立）"
        )
    return AuthResult(AuthStatus.ACCEPTED)


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))
