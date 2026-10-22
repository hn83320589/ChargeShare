"""範例資料：120 戶（15 層 × 8 戶）、每戶 1–2 張卡、8 支充電樁。"""

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from chargeshare.db.models import Card, Charger, Household
from chargeshare.security import hash_password

FLOORS, UNITS_PER_FLOOR = 15, 8
CHARGER_IDS = [f"CP{i:02d}" for i in range(1, 9)]


def unit_no(floor: int, unit: int) -> str:
    return f"{floor:02d}-{unit:02d}"


def card_id(floor: int, unit: int, index: int) -> str:
    return f"RFID-{floor:02d}{unit:02d}-{index}"


def demo_password(unit: str) -> str:
    """範例資料的住戶密碼（實際由管委會發放）。"""
    return f"pw-{unit}"


def seed_base(session: Session, hasher: Callable[[str], str] = hash_password) -> bool:
    """建立戶、卡片與充電樁；已有資料時不做事，回傳是否有建立。

    hasher 可替換成低成本版本，供測試快速建立資料。
    """
    if session.scalar(select(Household.id).limit(1)) is not None:
        return False
    for floor in range(1, FLOORS + 1):
        for unit in range(1, UNITS_PER_FLOOR + 1):
            number = unit_no(floor, unit)
            household = Household(unit_no=number, password_hash=hasher(demo_password(number)))
            cards = 2 if unit % 3 == 0 else 1
            household.cards = [Card(id_tag=card_id(floor, unit, i)) for i in range(1, cards + 1)]
            session.add(household)
    session.add_all(Charger(id=charger_id) for charger_id in CHARGER_IDS)
    session.flush()
    return True
